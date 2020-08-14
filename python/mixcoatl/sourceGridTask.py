import os
import numpy as np
from os.path import join
from scipy.spatial import distance
from astropy.io import fits

import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from lsst.obs.lsst import LsstCamMapper as camMapper
from lsst.obs.lsst.cameraTransforms import LsstCameraTransforms

from .sourcegrid import BaseGrid, DistortedGrid, grid_fit, coordinate_distances

camera = camMapper._makeCamera()
lct = LsstCameraTransforms(camera)

class SourceGridConfig(pexConfig.Config):
    """Configuration for GridFitTask."""

    max_displacement = pexConfig.Field("Maximum distance (pixels) between matched sources.",
                                       float, default=10.)
    nrows = pexConfig.Field("Number of grid rows.", int, default=49)
    ncols = pexConfig.Field("Number of grid columns.", int, default=49)
    y_kwd = pexConfig.Field("Source catalog y-position keyword", str, 
                            default='base_SdssShape_y')
    x_kwd = pexConfig.Field("Source catalog y-position keyword", str, 
                            default='base_SdssShape_x')
    yy_kwd = pexConfig.Field("Source catalog y-position keyword", str, 
                             default='base_SdssShape_yy')
    xx_kwd = pexConfig.Field("Source catalog y-position keyword", str, 
                             default='base_SdssShape_xx')
    flux_kwd = pexConfig.Field("Source catalog y-position keyword", str, 
                               default='base_SdssShape_instFlux')
    outfile = pexConfig.Field("Output filename", str, default="test.fits")

class SourceGridTask(pipeBase.Task):

    ConfigClass = SourceGridConfig
    _DefaultName = "SourceGridTask"

    @pipeBase.timeMethod
    def run(self, infile, optic_distortions_file=None):

        ## Obtain initial guess for grid center
        basename = os.path.basename(infile)
        projector_y = float(basename.split('_')[-1][:-5]) # camera x/y coords
        projector_x = float(basename.split('_')[-2][:-1])

        ccd_name, ccd_x, ccd_y = lct.focalMmToCcdPixel(projector_y, projector_x)

        x0_guess = 2*509*4. - ccd_x
        y0_guess = ccd_y

        src = fits.getdata(infile)

        ## Get source positions
        srcY = src['base_SdssShape_X']
        srcX = src['base_SdssShape_Y']

        ## Curate data here (remove bad shapes, fluxes, etc.)
        srcW = np.sqrt(np.square(src['base_SdssShape_XX']) + np.square(src['base_SdssShape_YY']))
        mask = (srcW > 4.)

        srcY = src['base_SdssShape_X'][mask]
        srcX = src['base_SdssShape_Y'][mask]

        ## Construct mask
        nsources = srcY.shape[0]
        indices, distances = coordinate_distances(srcY, srcX, srcY, srcX)
        nn_indices = indices[:, 1:5]
        nn_distances = distances[:, 1:5]
        med_dist = np.median(nn_distances)

        dist1_array = np.full(nsources, np.nan)
        dist2_array = np.full(nsources, np.nan)
        theta_array = np.full(nsources, np.nan)

        for i in range(nsources):

            yc = srcY[i]
            xc = srcX[i]

            for j in range(4):

                nn_dist = nn_distances[i, j]
                if np.abs(nn_dist - med_dist) > 10.: continue
                y_nn = srcY[nn_indices[i, j]]
                x_nn = srcX[nn_indices[i, j]]

                if x_nn > xc:
                    if y_nn > yc:
                        dist1_array[i] = nn_dist
                        theta_array[i] = np.arctan((y_nn-yc)/(x_nn-xc))
                    else:
                        dist2_array[i] = nn_dist

        ## Use theta to determine x/y step direction
        theta = np.nanmedian(theta_array)
        if theta >= np.pi/4.:
            theta = theta - (np.pi/2.)
            xstep = np.nanmedian(dist2_array)
            ystep = np.nanmedian(dist1_array)
        else:
            xstep = np.nanmedian(dist1_array)
            ystep = np.nanmedian(dist2_array)

        ## Optionally include optical distortions
        if optic_distortions_file is not None:
            pass # placeholder for now
        else:
            distortions = None

        ## Define fit parameters
        params = Parameters()
        params.add('ystep', value=ystep, vary=False)
        params.add('xstep', value=xstep, vary=False)
        params.add('theta', value=theta, vary=False)
        params.add('y0', value=y0_guess, min=y0_guess-ystep, max=y0_guess+ystep, vary=True, brute_step=ystep/4.)
        params.add('x0', value=x0_guess, min=x0_guess-xstep, max=x0_guess+xstep, vary=True, brute_step=xstep/4.)
        
        ## Optionally perform initial brute search
        brute = True # add to options
        if brute:
            params.add('y0', value=y0_guess, min=y0_guess-ystep, max=y0_guess+ystep, vary=True, brute_step=ystep/4.)
            params.add('x0', value=x0_guess, min=x0_guess-xstep, max=x0_guess+xstep, vary=True, brute_step=xstep/4.)
            minner = Minimizer(fit_error, params, fcn_args=(srcY, srcX, ncols, nrows, distortions))
            result = minner.minimize(method='brute', params=params)

            params = result.params   

        ## Enable parameter fit to theta
        vary_theta = True # add to options
        if vary_theta:
            params['theta'].set(vary=True)

        ## LM Fit
        minner = Minimizer(fit_error, params, fcn_args=(srcY, srcX, ncols, nrows, distortions))
        result = minner.minimize(params=params)
        
        ## Get source information
        srcY = src[self.config.y_kwd]
        srcX = src[self.config.x_kwd]
        srcXX = src[self.config.xx_kwd]
        srcYY = src[self.config.yy_kwd]
        srcF = src[self.config.flux_kwd]

        ## Construct fitted grid
        parvals = result.params.valuesdict()
        grid = BaseGrid(parvals['ystep'], parvals['xstep'], parvals['theta'], parvals['y0'], parvals['x0'],
                        ncols, nrows)

        gY, gX = grid.make_ideal_grid()

        ## Match detected sources to fitted grid
        indices, distances = coordinate_distances(gY, gX, srcY, srcX)
        nn_indices = indices[:, 0]

        ## Matched source information
        dy_array = srcY[nn_indices] - gY
        dx_array = srcX[nn_indices] - gX
        xx_array = np.zeros(gX.shape[0])
        yy_array = np.zeros(gY.shape[0])
        dxx_array = srcXX[nn_indices]
        dyy_array = srcYY[nn_indices]
        flux_array = np.zeros(gX.shape[0])
        dflux_array = srcF[nn_indices]

        ## Mask unmatched sources
        mask = np.hypot(dy_array, dx_array) >= self.config.max_displacement
        dx_array[mask] = np.nan
        dy_array[mask] = np.nan
        dxx_array[mask] = np.nan
        dyy_array[mask] = np.nan
        dflux_array[mask] = np.nan

        ## Construct source information dictionary
        data = {}
        data['X'] = gX
        data['Y'] = gY
        data['DX'] = dx_array
        data['DY'] = dy_array

        data['XX'] = xx_array
        data['YY'] = yy_array
        data['DXX'] = dxx_array
        data['DYY'] = dyy_array

        data['FLUX'] = flux_array
        data['DFLUX'] = dflux_array

        distorted_grid = DistortedGrid(grid.ystep, grid.xstep, grid.theta, 
                                       grid.y0, grid.x0, self.config.ncols, 
                                       self.config.nrows, data)
        distorted_grid.write_fits(self.config.outfile, overwrite=True)
