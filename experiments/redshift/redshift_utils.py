import numpy as np
import fitsio
import sys, os
from os.path import basename, splitext
sys.path.append("../..")
import planck
import scipy.integrate as integrate
from scipy import interpolate
from scipy.optimize import minimize
from funkyyak import grad, numpy_wrapper as np
import matplotlib.pyplot as plt

def sinc_interp(new_samples, samples, fvals, left=None, right=None):
    """
    Interpolates x, sampled at "s" instants
    Output y is sampled at "u" instants ("u" for "upsampled")

    from Matlab:
    http://phaseportrait.blogspot.com/2008/06/sinc-interpolation-in-matlab.html        
    """
    if len(fvals) != len(samples):
        raise Exception, 'function vals (fvals) and samples must be the same length'

    # Find the period  
    T = (samples[1:] - samples[:-1]).max()

    # sinc resample
    sincM = np.tile(new_samples, (len(samples), 1)) - \
            np.tile(samples[:, np.newaxis], (1, len(new_samples)))
    y = np.dot(fvals, np.sinc(sincM/T))

    # set outside values to left/right inputs if given
    if left is not None:
        y[new_samples < samples[0]] = np.nan
    if right is not None:
        y[new_samples > samples[-1]] = np.nan
    return y

def spline_interp(new_samples, samples, fvals):
    tck  = interpolate.splrep(samples, fvals, s=0)
    ynew = interpolate.splev(new_samples, tck, der=0)
    return ynew

def resample_rest_frame(spectra, spectra_ivar, zs, lam_obs, lam0):
    """ Resamples spectra with known red-shifts into rest frame 
        at samples given by lam0.
    """
    if lam_obs.ndim == 1:
        lam_obs = np.tile(lam_obs, (spectra.shape[0], 1))
    lam_mat                = np.zeros(spectra.shape)
    spectra_resampled      = np.zeros((spectra.shape[0], len(lam0)))
    spectra_ivar_resampled = np.zeros((spectra.shape[0], len(lam0)))
    for i in range(spectra.shape[0]):
        lam_mat[i, :] = lam_obs[i,:] / (1 + zs[i])
        spectra_resampled[i, :] = np.interp(x     = lam0,
                                            xp    = lam_mat[i, :],
                                            fp    = spectra[i, :],
                                            left  = np.nan,
                                            right = np.nan)
        # resample variances linearly, not inverse variances
        spec_var = 1. / spectra_ivar[i,:]
        spectra_ivar_resampled[i, :] = 1. / np.interp(x     = lam0,
                                                      xp    = lam_mat[i, :],
                                                      fp    = spec_var,
                                                      left  = np.nan,
                                                      right = np.nan)
    return spectra_resampled, spectra_ivar_resampled, lam_mat

def get_lam0(lam_subsample=10):
    """ Gets the lambda values from the spEigenQSO file, uses as fixed basis inputs """
    header     = fitsio.read_header('../../data/eigen_specs/spEigenQSO-55732.fits')
    eigQSOfits = fitsio.FITS('../../data/eigen_specs/spEigenQSO-55732.fits')
    lam0       = 10.**(header['COEFF0'] + np.arange(header['NAXIS1']) * header['COEFF1'])
    lam0       = lam0[::lam_subsample]
    lam0_delta = np.concatenate((lam0[1:] - lam0[:-1], [lam0[-1] - lam0[-2]]))
    eigQSO     = eigQSOfits[0].read()[:, ::lam_subsample]
    K          = eigQSO.shape[0]
    return lam0, lam0_delta

def load_data_clean_split(spec_fits_file = '../../andrew-qso.fits', Ntrain=500):

    # load and split
    fits_data = fitsio.FITS(spec_fits_file)

    # compute wavelength values
    log10lams   = fits_data[0].read()
    wavelengths = np.power(10, log10lams)

    # load red shift and weed out stars/high error ones
    quasar_spectra = fits_data[1].read()
    quasar_ivar    = fits_data[2].read()
    meta_data      = fits_data[3].read()
    quasar_z       = meta_data['Z']
    quasar_zerr    = meta_data['Z_ERR']

    # remove stars/low red shift objs
    bad_idx = (quasar_z < .01) | (np.abs(quasar_zerr) > 1e-2)
    quasar_spectra = quasar_spectra[~bad_idx, :]
    quasar_ivar    = quasar_ivar[~bad_idx, :]
    quasar_z       = quasar_z[~bad_idx]
    quasar_zerr    = quasar_zerr[~bad_idx]

    # split train/test
    np.random.seed(42)
    perm = np.random.permutation(quasar_spectra.shape[0])
    train_idx = perm[0:Ntrain]
    test_idx  = perm[Ntrain:]

    trainObj = {}
    trainObj['spectra']      = quasar_spectra[train_idx, :]
    trainObj['spectra_ivar'] = quasar_ivar[train_idx, :]
    trainObj['Z']            = quasar_z[train_idx]
    trainObj['Z_err']        = quasar_zerr[train_idx]

    testObj = {}
    testObj['spectra']      = quasar_spectra[test_idx, :]
    testObj['spectra_ivar'] = quasar_ivar[test_idx, :]
    testObj['Z']            = quasar_z[test_idx]
    testObj['Z_err']        = quasar_zerr[test_idx]
    return wavelengths, trainObj, testObj

def load_sdss_fluxes_clean_split(Ntest=500, seed=123):
    """ Loads in the fluxes (which are stored in "Magnitudes",
        from the dr7qso dump in data.  It then converts them to nanomaggies
        and returns them.
        TODO: add a flag for nanomaggie or photon count values...
    """
    acm_file = "/Users/acm/Dropbox/Proj/astro/DESIMCMC/data/DR10QSO/DR10Q_v2.fits"
    if os.path.exists(acm_file):
        qso_file = acm_file
    else: 
        print "attempting relative path load"
        qso_file = "../../data/DR10QSO/DR10Q_v2.fits"

    ## load and read quasar fluxes
    fits_data  = fitsio.FITS(qso_file)
    qso_data   = fits_data[1].read()
    mag_fields = ['UMAG', 'GMAG', 'RMAG', 'IMAG', 'ZMAG']
    mag_errs   = [m + 'ERR' for m in mag_fields]
    qso_mags   = np.column_stack([qso_data[m] for m in mag_fields])

    # remove questionable mags
    outlier_idx, outlier_field = np.where(qso_mags < 1)
    mask              = np.ones(qso_mags.shape[0], dtype=bool)
    mask[outlier_idx] = False
    qso_mags          = qso_mags[mask, :]
    Nquasar           = qso_mags.shape[0]

    # magnitude values => nanomaggies
    qso_nanomaggies = mags2nanomaggies(qso_mags)

    ## split/train test
    np.random.seed(seed)
    perm = np.random.permutation(Nquasar)
    train_idx = perm[:(Nquasar-Ntest)]
    test_idx  = perm[(Nquasar-Ntest):]

    ## return train/test objects
    trainObj = {}
    trainObj['sdss_fluxes'] = qso_nanomaggies[train_idx, :]
    trainObj['sdss_mags']   = qso_mags[train_idx, :]
    trainObj['Z'] = qso_data['z'][train_idx]

    testObj = {}
    testObj['sdss_fluxes'] = qso_nanomaggies[test_idx, :]
    testObj['sdss_mags']   = qso_mags[test_idx, :]
    testObj['Z'] = qso_data['z'][test_idx]
    return trainObj, testObj

def mags2nanomaggies(mags): 
    return np.power(10., (mags - 22.5)/-2.5)


def load_specs_from_disk(spec_files): 
    # 0. load spectra from fits files
    bad_ids    = []

    unique_lams = np.array([])
    spec_ids    = []
    spec_fluxes = []
    spec_lams   = []
    spec_ivars  = []
    spec_mods   = []
    for i in range(len(spec_files)):
        if i % 20 == 0: 
            sys.stdout.write("\r  load_specs_from_disk ... (spec %d of %d)" % (i, len(spec_files)))
            sys.stdout.flush()

        # load spec info
        try: 
            sdf = fitsio.FITS(spec_files[i])
        except:
            bad_ids.append(i)
            continue
        spec_flux = sdf[1]['flux'].read()
        spec_ivar = sdf[1]['ivar'].read()
        spec_lam  = np.power(10., sdf[1]['loglam'].read())
        spec_mod  = sdf[1]['model'].read()
        unique_lams = np.unique(np.concatenate((unique_lams, spec_lam)))

        # store list so we don't have to hit the disk again
        spec_lams.append(spec_lam)
        spec_fluxes.append(spec_flux)
        spec_ivars.append(spec_ivar)
        spec_mods.append(spec_mod)
        spec_ids.append( basename( splitext(spec_files[i])[0] ) )

    ## put everything in one big lam_obs
    spec_grid = np.zeros((len(spec_fluxes), len(unique_lams)))
    spec_ivar_grid = np.zeros((len(spec_fluxes), len(unique_lams)))
    spec_mod_grid = np.zeros((len(spec_fluxes), len(unique_lams)))
    for i in range(len(spec_fluxes)):
        start_i = np.where(unique_lams==spec_lams[i][0])[0][0]
        end_i   = np.where(unique_lams==spec_lams[i][-1])[0][0]+1
        spec_grid[i, start_i:end_i]      = spec_fluxes[i]
        spec_ivar_grid[i, start_i:end_i] = spec_ivars[i]
        spec_mod_grid[i, start_i:end_i] = spec_ivars[i]

    return spec_grid, spec_ivar_grid, spec_mod_grid, unique_lams, spec_ids, bad_ids


# precomputed 10^((48.6 - 2.5*17 + 22.5)/2.5)
flux_constant = 275422870333.81744384765625
def project_to_bands(spectra, wavelengths): 
    fluxes = np.zeros(5)
    for i, band in enumerate(['u','g','r','i','z']):
        # interpolate sensitivity curve onto wavelengths
        sensitivity = np.interp(wavelengths, planck.wavelength_lookup[band]*(10**10), 
                                             planck.sensitivity_lookup[band])
        norm        = sum(sensitivity)
        # conversion
        flambda2fnu  = wavelengths**2 / 2.99792e18
        fthru        = np.sum(sensitivity * spectra * flambda2fnu) / norm 
        #mags         = -2.5 * np.log10(fthru) - (48.6 - 2.5*17)
        #fluxes[i]    = np.power(10., (mags - 22.5)/-2.5)
        # We don't have to log and exponentiate 
        fluxes[i] = fthru * flux_constant 
    return fluxes

def fit_weights_given_basis(B, lam0, X, inv_var, z_n, lam_obs, return_loss=False, sgd_iter=100):
    """ Weighted optimization routine to fit the values of \log w given 
    basis B. 
    """
    #convert spec_n to lam0
    spec_n_resampled = np.interp(lam0, lam_obs/(1+z_n), X, left=np.nan, right=np.nan)
    ivar_n_resampled = np.interp(lam0, lam_obs/(1+z_n), inv_var, left=np.nan, right=np.nan)
    spec_n_resampled[np.isnan(spec_n_resampled)] = 0.0
    ivar_n_resampled[np.isnan(ivar_n_resampled)] = 0.0
    def loss_omegas(omegas):
        """ loss over weights with respect to fixed basis """
        ll_omega = .5 / (100.) * np.sum(np.square(omegas))
        Xtilde   = np.dot(np.exp(omegas), B)
        return np.sum(ivar_n_resampled * np.square(spec_n_resampled - Xtilde)) + ll_omega
    loss_omegas_grad = grad(loss_omegas)

    # first wail on it with gradient descent/momentum
    omegas        = .01*np.random.randn(B.shape[0])
    momentum      = .9
    learning_rate = 1e-4
    cur_dir = np.zeros(omegas.shape)
    lls     = np.zeros(sgd_iter)
    for epoch in range(sgd_iter):
        grad_th    = loss_omegas_grad(omegas)
        cur_dir    = momentum * cur_dir + (1.0 - momentum) * grad_th
        omegas    -= learning_rate * cur_dir
        lls[epoch] = loss_omegas(omegas)

        step_mag = np.sqrt(np.sum(np.square(learning_rate*cur_dir)))
        if epoch % 20 == 0:
            print "{0:15}|{1:15}|{2:15}".format(epoch, "%7g"%lls[epoch], "%2.4f"%step_mag)

    # tighten it up w/ LBFGS
    res = minimize(x0 = omegas,
                   fun = loss_omegas,
                   jac=loss_omegas_grad,
                   method = 'L-BFGS-B',
                   options = { 'disp': True, 'maxiter': 10000 })

    # return the loss function handle as well - for debugging
    if return_loss:
        return np.exp(res.x), loss_omegas
    return np.exp(res.x)

def evaluate_random_direction(fun, x0, n=100, delta=.1):
    """ plots a multivariate function over one (random) direction """
    # random direciton w/ magnitude delta
    param_scale = .1
    rand_dir = np.random.randn(x0.size) * param_scale
    rand_dir = delta * rand_dir / np.sqrt(np.dot(rand_dir, rand_dir))
    # bounds
    x_left  = x0 - n*rand_dir
    ll_grid = np.zeros(2*n+1)
    x = x_left
    for n in range(len(ll_grid)):
        x = x_left + n*rand_dir
        ll_grid[n] = fun(x)
    return ll_grid

def check_grad(fun, jac, th):
    """ check the gradient along a random direction """
    param_scale = .1
    rand_dir    = np.random.randn(th.size) * param_scale
    rand_dir    = rand_dir / np.sqrt(np.dot(rand_dir, rand_dir))
    test_fun    = lambda x : fun(th + x * rand_dir.reshape(th.shape))
    nd          = (test_fun(1e-4) - test_fun(-1e-4)) / 2e-4
    ad          = np.dot(jac(th).ravel(), rand_dir)
    print "Checking grads. Relative diff is: {0}".format((nd - ad)/np.abs(nd))

def softmax(x):
    x_tilde = np.exp(x)
    return x_tilde / x_tilde.sum()

class ParamParser(object):
    """ Helper class to handle different slicing for different parameters
    in one long vector """
    def __init__(self):
        self.idxs_and_shapes = {}
        self.N = 0

    def add_weights(self, name, shape):
        start = self.N
        self.N += np.prod(shape)
        self.idxs_and_shapes[name] = (slice(start, self.N), shape)

    def get(self, vect, name):
        idxs, shape = self.idxs_and_shapes[name]
        return np.reshape(vect[idxs], shape)

    def set(self, vect, name, val):
        idxs, shape = self.idxs_and_shapes[name]
        vect[idxs] = val.ravel()

    def get_slice(name):
        return self.idxs_and_shapes[name][0]


