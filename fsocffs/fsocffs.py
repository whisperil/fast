import numpy
from . import funcs
from . import ao_power_spectra
from aotools import circle, cn2_to_r0, isoplanaticAngle, coherenceTime
from astropy.io import fits
from tqdm import tqdm

class FFS():
    def __init__(self, params):
        '''
        Initialise the simulation with a set of parameters.

        TODO: Check that all required params are given at initialisation

        Parameters:
            params (dict): Simulation parameters
        '''
        self.params = params
        self.Niter = params['NITER']
        self.Nchunks = params['NCHUNKS']
        self.fftw = params['FFTW']
        self.temporal = params['TEMPORAL']
        self.dt = params['DT']

        if self.Niter % self.Nchunks != 0:
            raise Exception('NCHUNKS must divite NITER without remainder')
        else:
            self.Niter_per_chunk = self.Niter // self.Nchunks

        self.init_atmos(self.params)
        self.init_frequency_grid(self.params)
        self.init_beam_params(self.params)
        self.init_ao_params(self.params)
        self.init_pupil_mask(self.params)

        self.compute_powerspec()

    def run(self):
        self.compute_scrns()
        I = self.compute_I()
        return I

    def init_frequency_grid(self, params):
        if params['DX'] is 'auto':
            self.dx = params['DSUBAP'] / 2 # Nyquist sample WFS subaperture
        else:
            self.dx = params['DX']
        
        if params['NPXLS'] is 'auto':
            nyq_paa = numpy.pi / (self.h[-1] * self.paa/206265.) # nyquist sampling of highest spatial frequency required ()
            nyq_temp = numpy.pi / (max(self.wind_speed) * params['TLOOP'])
            nyq = numpy.min([nyq_paa, nyq_temp])
            nyq_Npxls = int(2*numpy.ceil(2*numpy.pi/(nyq * self.dx)/2)) # ensure even
            ap_Npxls = int(2*numpy.ceil(params['Tx']/self.dx/2))
            self.Npxls = numpy.max([nyq_Npxls, ap_Npxls])
        else:
            self.Npxls = params['NPXLS']

        self.freq = SpatialFrequencies(self.Npxls, self.dx)

        self.subharmonics = params['SUBHARM']

        if self.subharmonics:
            self.freq.make_subharm_freqs()

        if self.temporal:
            self.freq.make_temporal_freqs(len(self.h), self.Npxls, self.Niter_per_chunk,
                self.wind_speed, self.wind_dir, self.dt)

    def init_atmos(self, params):
        self.zenith_correction = self.calc_zenith_correction(params['ZENITH_ANGLE'])
        self.h = params['H_TURB'] * self.zenith_correction
        self.cn2 = params['CN2_TURB'] * self.zenith_correction
        self.L = params['H_SAT'] * self.zenith_correction
        self.wind_speed = params['WIND_SPD']
        self.wind_dir = params['WIND_DIR']
        self.dtheta = params['DTHETA']
        self.paa = numpy.sqrt(self.dtheta[0]**2 + self.dtheta[1]**2)
        self.wind_vector = (self.wind_speed * 
            numpy.array([numpy.cos(numpy.radians(self.wind_dir)),
                         numpy.sin(numpy.radians(self.wind_dir))])).T

        self.r0 = cn2_to_r0(params['CN2_TURB'].sum(), lamda=500e-9)
        self.theta0 = isoplanaticAngle(params['CN2_TURB'], params['H_TURB'], lamda=500e-9)
        self.tau0 = coherenceTime(params['CN2_TURB'], params['WIND_SPD'], lamda=500e-9)
        self.L0 = params['L0']
        self.l0 = params['l0']

    def init_beam_params(self, params):
        self.W0 = params['W0']
        self.F0 = params['F0']
        self.wvl = params['WVL']
        self.k = 2*numpy.pi/self.wvl

        self.Theta_0, self.Lambda_0, self.Theta, self.Lambda, self.Theta_bar = \
            funcs.calc_gaussian_beam_parameters(self.L, params['F0'], params['W0'], params['WVL'])
        self.W = params['W0'] * numpy.sqrt(self.Theta_0**2 + self.Lambda_0**2)

        self.Tx = params['Tx']
        self.Tx_obsc = params['Tx_obsc']
        self.Rx = params['Rx']

    def init_ao_params(self, params):
        self.ao_mode = params['AO_MODE']
        self.Dsubap = params['DSUBAP']
        self.tloop = params['TLOOP']
        self.texp = params['TEXP']
        self.Zmax = params['ZMAX']
        self.alias = params['ALIAS']
        self.noise = params['NOISE']
        self.modal = params['MODAL']
        self.modal_mult = params['MODAL_MULT']
        self.Gtilt = params['GTILT']

        if self.ao_mode == 'TT_PA':
            # force modal correction with tip/tilt
            self.Zmax = 3
            self.modal = True
            self.modal_mult = 1

        self.lf_mask = ao_power_spectra.mask_lf(self.freq.main, self.Dsubap, 
                    modal=self.modal, modal_mult=self.modal_mult, Zmax=self.Zmax, 
                    D=self.Tx, Gtilt=self.Gtilt)
        self.hf_mask = 1 - self.lf_mask

        if self.subharmonics:
            self.lf_mask_subharm = ao_power_spectra.mask_lf(self.freq.main,
                    self.Dsubap, modal=self.modal, modal_mult=self.modal_mult, Zmax=self.Zmax,
                    D=self.Tx, Gtilt=self.Gtilt)

        if self.temporal:
            self.lf_mask_temporal = ao_power_spectra.mask_lf(self.freq.main,
                    self.Dsubap, modal=self.modal, modal_mult=self.modal_mult, Zmax=self.Zmax,
                    D=self.Tx, Gtilt=self.Gtilt)

    def init_pupil_mask(self, params):
        if params['PROP_DIR'] is 'up':
            # Gaussian aperture
            if params['AXICON']:
                ptype = 'axicon'
            else:
                ptype = 'gauss'

            self.pupil = funcs.compute_pupil(self.Npxls, self.dx, params['Tx'], 
                params['W0'], params['Tx_obsc'], ptype=ptype)
        else:
            # Circular (fully illuminated) aperture
            self.pupil = funcs.compute_pupil(self.Npxls, self.dx, params['Tx'], 
                Tx_obsc=params['Tx_obsc'], ptype='circ')

        if params['SMF']:
            # compute optimal SMF parameters
            self.smf = True
            self.fibre_efield = funcs.optimize_fibre(self.pupil, self.dx)
        else:
            self.smf = False
            self.fibre_efield = 1.

        return self.pupil

    def compute_powerspec(self):
        self.turb_powerspec = funcs.turb_powerspectrum_vonKarman(
            self.freq.main, self.cn2, self.L0, self.l0, C=self.params['C'])

        self.G_ao = ao_power_spectra.G_AO_Jol(
            self.freq.main, self.lf_mask, self.ao_mode, self.h, 
            self.wind_vector, self.dtheta, self.Tx, self.wvl, self.Zmax, 
            self.tloop, self.texp)

        if self.alias:
            self.alias_powerspec = ao_power_spectra.Jol_alias_openloop(
                self.freq.main, self.Dsubap, self.cn2, self.lf_mask, self.wind_vector,
                self.texp, self.wvl, 10, 10, self.L0, self.l0)
        else:
            self.alias_powerspec = 0.

        if self.noise > 0:
            self.noise_powerspec = ao_power_spectra.Jol_noise_openloop(
                self.freq.main, self.Dsubap, self.noise, self.lf_mask)
        else:
            self.noise_powerspec = 0.

        self.powerspec = 2 * numpy.pi * self.k**2 * \
            funcs.integrate_path((self.turb_powerspec * self.G_ao + self.alias_powerspec), h=self.h, layer=self.params['LAYER']) \
            + self.noise_powerspec

        if self.subharmonics:
            self.turb_lo = funcs.turb_powerspectrum_vonKarman(
                self.freq.subharm, self.cn2, self.L0, self.l0, C=self.params['C'])

            self.G_ao_lo = ao_power_spectra.G_AO_Jol(
                self.freq.subharm, self.lf_mask_subharm, 
                self.ao_mode, self.h, self.wind_vector, self.dtheta, self.Tx, self.wvl, self.Zmax, 
                self.tloop, self.texp, self.Dsubap, self.modal, self.modal_mult)

            if self.alias:
                self.alias_subharm = ao_power_spectra.Jol_alias_openloop(
                    self.freq.subharm, self.Dsubap, 
                    self.cn2, self.lf_mask_subharm, self.wind_vector, self.texp, self.wvl, 10, 10,
                    self.L0, self.l0)
            else:
                self.alias_subharm = 0.

            if self.noise > 0:
                self.noise_subharm = ao_power_spectra.Jol_noise_openloop(
                    self.freq.subharm,
                    self.Dsubap, self.noise, self.lf_mask_subharm)
            else:
                self.noise_subharm = 0.

            self.powerspec_subharm = 2 * numpy.pi * self.k**2 * \
                funcs.integrate_path(self.turb_lo * self.G_ao_lo + self.alias_subharm, h=self.h, layer=self.params['LAYER']) \
                + self.noise_subharm 
        else:
            self.powerspec_subharm = None

        if self.temporal:
            self.turb_temporal = funcs.turb_powerspectrum_vonKarman(
                self.freq.temporal, self.cn2, self.L0, self.l0, C=self.params['C'])

            self.G_ao_temporal = ao_power_spectra.G_AO_Jol(
                self.freq.temporal, self.lf_mask_temporal, 
                self.ao_mode, self.h, self.wind_vector, self.dtheta, self.Tx, self.wvl, self.Zmax, 
                self.tloop, self.texp, self.Dsubap, self.modal, self.modal_mult)

            if self.alias:
                self.alias_temporal = ao_power_spectra.Jol_alias_openloop(
                    self.freq.temporal, self.Dsubap, 
                    self.cn2, self.lf_mask_temporal, self.wind_vector, self.texp, self.wvl, 10, 10,
                    self.L0, self.l0)
            else:
                self.alias_temporal = 0.

            if self.noise > 0:
                self.noise_temporal = ao_power_spectra.Jol_noise_openloop(
                    self.freq.temporal, 
                    self.Dsubap, self.noise, self.lf_mask_temporal)
            else:
                self.noise_temporal = 0.

            temporal_powerspec_beforeintegration = 2 * numpy.pi * self.k**2 * \
                funcs.integrate_path(self.turb_temporal * self.G_ao_temporal + self.alias_temporal, h=self.h, layer=self.params['LAYER']) \
                + self.noise_temporal

            # integrate along y axis
            self.temporal_powerspec = temporal_powerspec_beforeintegration.sum(-2) * self.freq.df

        else:
            self.temporal_powerspec = None

    def compute_scrns(self):

        self.phs = numpy.zeros((self.Niter, *self.powerspec.shape))

        for i in tqdm(range(self.Nchunks)):
            self.phs[i*self.Niter_per_chunk:(i+1)*self.Niter_per_chunk] = funcs.make_phase_fft(
                self.Niter_per_chunk, self.freq, self.powerspec, self.subharmonics, self.powerspec_subharm, 
                self.dx, self.fftw, self.temporal, self.temporal_powerspec)

        return self.phs

    def compute_I(self, pupil=None):
        if pupil is None:
            pupil = self.pupil * self.fibre_efield

        logamp_var = funcs.logamp_var(pupil, self.freq.main, self.dx, self.h, self.cn2, self.wvl,
            self.L0, self.l0)
        self.rand_logamp = numpy.random.normal(
            loc=0, scale=numpy.sqrt(logamp_var), size=(self.Niter,))

        phase_component = (pupil * numpy.exp(1j * self.phs)).sum((1,2)) * self.dx**2

        self.diffraction_limit = numpy.abs(pupil.sum() * self.dx**2)**2

        self.I = numpy.exp(2 * self.rand_logamp) * numpy.abs(phase_component)**2

        if self.params['PROP_DIR'] is 'up':
            # Far field intensity
            self.I /= (self.wvl * self.L)**2
            self.diffraction_limit /= (self.wvl * self.L)**2

        return self.I

    def calc_zenith_correction(self, zenith_angle):
        zenith_angle_rads = numpy.radians(zenith_angle)
        gamma = 1/numpy.cos(zenith_angle_rads)
        return gamma

    def make_header(self, params):
        hdr = fits.Header()
        hdr['ZENITH'] = params['ZENITH_ANGLE']
        hdr['WVL'] = int(params['WVL']*1e9)
        if numpy.isinf(params['L0']):
            hdr['OTRSCALE'] = str(params['L0'])
        else:
            hdr['OTRSCALE'] = params['L0']
        hdr['INRSCALE'] = params['l0']
        hdr['POWER'] = params['POWER'] 
        hdr['PAA'] = self.paa
        hdr['TLOOP'] = params['TLOOP'] 
        hdr['TEXP'] = params['TEXP']
        hdr['DSUBAP'] = params['DSUBAP'] 
        hdr['ALIAS'] = str(params['ALIAS'])
        hdr['NOISE'] = params['NOISE']
        hdr['TX'] = params['Tx']
        hdr['TX_OBSC'] = params['Tx_obsc']
        hdr['AXICON'] = str(params['AXICON'])
        hdr['H_SAT'] = params['H_SAT']
        hdr['DX'] = self.dx
        hdr['NPXLS'] = self.Npxls
        hdr['NITER'] = self.Niter
        hdr['R0'] = self.r0
        hdr['THETA0'] = self.theta0
        hdr['TAU0'] = self.tau0
        return hdr

    def save(self, fname, **kwargs):
        hdr = self.make_header(self.params) 
        fits.writeto(fname, self.I, header=hdr, **kwargs)

class SpatialFrequencies():

    def __init__(self, N, dx):

        self.main = SpatialFrequencyStruct()
        self.subharm = SpatialFrequencyStruct()
        self.temporal = SpatialFrequencyStruct()

        self.N = N
        self.dx = dx

        self.main.fx, self.main.fy, self.main.fabs, self.main.f = funcs.f_grid_dx(N, dx)
        self.main.df = self.main.f[1] - self.main.f[0]

        # make main spatial frequencies attributes of Frequencies object too
        self.fx = self.main.fx
        self.fy = self.main.fy
        self.fabs = self.main.fabs
        self.f = self.main.f
        self.df = self.main.df

    def make_subharm_freqs(self, pmax=3):

        self.subharm.fx = numpy.zeros((pmax,3,3))
        self.subharm.fy = numpy.zeros((pmax,3,3))
        self.subharm.fabs = numpy.zeros((pmax,3,3))
        D = self.dx * self.N
        for i,p in enumerate(range(1,pmax+1)):
            df_lo = 2*numpy.pi/(3**p * D)
            fx_lo = numpy.arange(-1,2) * df_lo
            fx_lo, fy_lo = numpy.meshgrid(fx_lo, fx_lo)
            fabs_lo = numpy.sqrt(fx_lo**2 + fy_lo**2)
            self.subharm.fx[i] = fx_lo
            self.subharm.fy[i] = fy_lo
            self.subharm.fabs[i] = fabs_lo

    def make_temporal_freqs(self, nlayer, Ny, Nx, wind_speed, wind_dir, dt):

        self.temporal.fx = numpy.zeros((nlayer, Ny, Nx))
        self.temporal.fy = numpy.zeros((nlayer, Ny, Nx))
        
        for i in range(nlayer):
            dx = wind_speed[i] * dt
            df_temporal = 2 * numpy.pi / (Nx * dx)

            # define x axis according to temporal requirements, and y axis 
            # same as the main y axis, since we will integrate over this one
            fx_axis = numpy.arange(-Nx/2, Nx/2) * df_temporal
            fy_axis = self.main.f
            fx, fy = numpy.meshgrid(fx_axis, fy_axis)

            # rotate the fx and fy so wind along x axis
            theta = numpy.radians(wind_dir[i])
            fx_rot = fx * numpy.cos(theta) - fy * numpy.sin(theta)
            fy_rot = fx * numpy.sin(theta) + fy * numpy.cos(theta)

            self.temporal.fx[i] = fx_rot
            self.temporal.fy[i] = fy_rot

        self.temporal.fabs = numpy.sqrt(self.temporal.fx**2 + self.temporal.fy**2)

class SpatialFrequencyStruct():

    fx = None
    fy = None
    fabs = None
    f = None
    df = None