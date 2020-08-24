import glob
import numpy as np
import argparse
import os
import scipy
from scipy.spatial import distance
from os.path import join
import multiprocessing as mp
import pickle

from lsst.obs.lsst import LsstCamMapper as camMapper
from lsst.obs.lsst.cameraTransforms import LsstCameraTransforms, channelToAmp, getAmpImage

from astropy.io import fits
from scipy import optimize

camera = camMapper._makeCamera()
lct = LsstCameraTransforms(camera)

class SpotFitter:

    def __init__(self, max_displacement=10., output_dir='./'):
        
        self.output_dir = output_dir
        self.max_displacement = max_displacement

    def run(self, infile):

        basename = os.path.basename(infile)
        projector_y = float(basename.split('_')[-1][:-5]) # these are camera x/y coords
        projector_x = float(basename.split('_')[-2][:-1])

        ccd_name, ccd_x, ccd_y = lct.focalMmToCcdPixel(projector_y, projector_x)

        ## This looks good for R02 S02
        x_guess = 2*509*4. - ccd_x - 27.0
        y_guess = ccd_y - 67.0

        ## Get model and data source positions
        src = fits.getdata(infile)
        model_grid = SourceGrid.from_source_catalog(src, y_guess=y_guess, x_guess=x_guess, 
                                                    mean_func= np.nanmedian)
        gY, gX = model_grid.make_grid(nrows=49, ncols=49)

        srcX = src['base_SdssShape_x']
        srcY = src['base_SdssShape_y']
        srcXX = src['base_SdssShape_xx']
        srcYY = src['base_SdssShape_yy'] 
        srcF = src['base_SdssShape_instFlux']

        ## Calculate nearest neighbor distance to each model source
        indices, distances = coordinate_distances(gY, gX, srcY, srcX)
        nn_indices = indices[:, 0]
        dx_array = srcX[nn_indices]-gX
        dy_array = srcY[nn_indices]-gY
        xx_array = srcXX[nn_indices]
        yy_array = srcYY[nn_indices]
        flux_array = srcF[nn_indices]

        ## Threshold cut for unmatched sources
        indices = np.hypot(dx_array, dy_array) >= self.max_displacement
        dx_array[indices] = np.nan
        dy_array[indices] = np.nan
        xx_array[indices] = np.nan
        yy_array[indices] = np.nan
        flux_array[indices] = np.nan

        ## Output FITs construction
        hdr = fits.Header()
        hdr['X0'] = model_grid.x0
        hdr['Y0'] = model_grid.y0
        hdr['DX'] = model_grid.dx
        hdr['DY'] = model_grid.dy
        hdr['THETA'] = model_grid.theta

        columns = [fits.Column('DATA_DX', array=dx_array, format='D'),
                   fits.Column('DATA_DY', array=dy_array, format='D'),
                   fits.Column('MODEL_X', array=gX, format='D'),
                   fits.Column('MODEL_Y', array=gY, format='D'),
                   fits.Column('DATA_XX', array=xx_array, format='D'),
                   fits.Column('DATA_YY', array=yy_array, format='D'),
                   fits.Column('DATA_Flux', array=flux_array, format='D')]

        hdu = fits.BinTableHDU.from_columns(columns, header=hdr)
        outfile = join(self.output_dir, '{0}_raw_displacement_data.fits'.format(os.path.splitext(basename)[0]))
        hdu.writeto(outfile, overwrite=True)

class SourceGrid:
    """Infinite undistorted grid of points.
    
    Grid is defined by grid spacing in two orthogonal directions and 
    a rotation angle of the grid lines with respect to the x-axis.
       
    Attributes:
        dy: Grid spacing in the vertical direction.
        dx: Grid spacing in the horizontal direction.
        theta: Angle [rads] between the grid and global horizontal directions          
    """
    
    def __init__(self, dy, dx, theta, y0, x0):
        """Initializes SourceGrid class with grid parameters."""
        
        self.dy = dy
        self.dx = dx
        self.theta = theta
        self.y0 = y0
        self.x0 = x0
    
    @classmethod
    def from_source_catalog(cls, source_catalog, y_guess=0.0, x_guess=0.0, 
                            y_kwd='base_SdssCentroid_y', 
                            x_kwd='base_SdssCentroid_x', 
                            mean_func=np.nanmean, ncols=49, nrows=49, 
                            **kwargs):
        """Generate a source grid model from a given source catalog.
    
        Args:
            source_catalog: Source catalog table.
            y_kwd: Keyword to access y-position information from source catalog.
            x_kwd: Keyword to access x-position information from source catalog.
        
        Returns:
            SourceGrid object with grid parameters calculated from the input
            source catalog.
        """
    
        ## Get x/y coordinate arrays
        y_raw = source_catalog[y_kwd]
        x_raw = source_catalog[x_kwd]
        y, x = remove_nan_coords(y_raw, x_raw)
        nsources = y.shape[0] 
        
        ## Calculate median distance to 4 nearest neighbors
        indices, distances = coordinate_distances(y, x, y, x)
        nn_indices = indices[:, 1:5]
        nn_distances = distances[: , 1:5]
        med_dist = np.median(nn_distances)
    
        ## Arrays to hold results (default is NaN)
        dist1_array = np.full(nsources, np.nan)
        dist2_array = np.full(nsources, np.nan)
        theta_array = np.full(nsources, np.nan)
    
        ## Calculate grid rotation
        for i in range(nsources):

            ## For each source, get 4 nearest neighbors
            y0 = y[i]
            x0 = x[i]
        
            for j in range(4):
            
                nn_dist = nn_distances[i, j] 
                if np.abs(nn_dist  - med_dist) > 10.: continue                
                y1 = y[nn_indices[i, j]]
                x1 = x[nn_indices[i, j]]
    
                ## Use the nearest neighbor in Quadrant 1 (with respect to source)
                if x1 > x0:
                    if y1 > y0:
                        dist1_array[i] = nn_dist
                        theta_array[i] = np.arctan((y1-y0)/(x1-x0))
                    else:
                        dist2_array[i] = nn_dist
                    
        ## Take mean over all sources (ignoring NaN entries)
        theta = mean_func(theta_array)
        if theta >= np.pi/4.:
            theta = theta - (np.pi/2.)
            dx = mean_func(dist2_array)
            dy = mean_func(dist1_array)
        else:
            dx = mean_func(dist1_array)
            dy = mean_func(dist2_array)
                
        ## Perform constrained fit to determine y0, x0
        guess = [dy, dx, theta, y_guess, x_guess]
        bounds = [(dy, dy), (dx, dx), (theta, theta),
                  (None, None), (None, None)]
        
        fit_results = scipy.optimize.minimize(grid_fit_error, guess, 
                                              args=(y, x, ncols, nrows), 
                                              method='SLSQP', bounds=bounds)    

        return cls(*fit_results.x)
        
    def make_grid(self, nrows=49, ncols=49):
        """Create x/y coordinate arrays for (nrows x ncols) grid of sources."""
        
        ## Create a standard nrows x ncols grid of points
        y_array = np.asarray([n*self.dy - (nrows-1)*self.dy/2. for n in range(nrows)])
        x_array = np.asarray([n*self.dx - (ncols-1)*self.dx/2. for n in range(ncols)])
        Y, X = np.meshgrid(y_array, x_array)
        
        ## Rotate grid using rotation matrix
        Xr = np.cos(self.theta)*X - np.sin(self.theta)*Y
        Yr = np.sin(self.theta)*X + np.cos(self.theta)*Y
        
        ## Move center of grid to desired x/y center coordinates
        Xr += self.x0
        Yr += self.y0
        
        ## Return the flattened arrays
        return Yr.flatten(), Xr.flatten()

def remove_nan_coords(y, x):
    """Remove NaN entries for y/x position arrays.
    
    Args:
        y: Array of source y-positions.
        x: Array of source x-positions.
        
    Returns:
        Tuple of y/x positional arrays, removing any NaN 
        from the array.
    """
    
    y_new = y[~np.isnan(y) & ~np.isnan(x)]
    x_new = x[~np.isnan(y) & ~np.isnan(x)]
    
    return y_new, x_new
    
def coordinate_distances(y0, x0, y1, x1, metric='euclidean'):
    
    coords0 = np.stack([y0, x0], axis=1)
    coords1 = np.stack([y1, x1], axis=1)
    
    ## Calculate distances to all points
    distance_array = distance.cdist(coords0, coords1, metric=metric)
    indices = np.argsort(distance_array, axis=1)
    distances = np.sort(distance_array, axis=1)
    
    return indices, distances
    
def mask_by_ccd_geom(y, x, ccd_geometry):
    """Mask out source positions outside of CCD boundaries.
    
    Args:
        y: Array of source y-positions.
        x: Array of source x-positions.
        x_bounds: Tuple describing min/max CCD extent in x-direction.
        y_bounds: Tuple describing min/max CCD extent in y-direction.
        
    Returns:
        Tuple of y/x positional arrays, removing entries outside
        of CCD boundaries.
    """
    
    min_y = ccd_geometry['min_y']
    max_y = ccd_geometry['max_y']
    min_x = ccd_geometry['min_x']
    max_x = ccd_geometry['max_x']
    
    y_new = y[(min_y < y) & (y <= max_y) & (min_x < x) & (x <= max_x)]
    x_new = x[(min_y < y) & (y <= max_y) & (min_x < x) & (x <= max_x)]
    
    return y_new, x_new
    
def grid_fit_error(params, src_y, src_x, nrows, ncols):
    """Calculate sum of positional errors of true source grid and model grid.
    
    For every true source, the distance to the nearest neighbor source 
    from a model source grid is calculated.  The mean for every true source
    is taken as the fit error between true source grid and model grid.
       
    Args:
        x: Iterable of grid parameters.
        src_Y: Array of source y-positions.
        src_X: Array of source x-positions.
        maxY: Maximum y-value given CCD vertical size.
        maxX: Maximum x-value given CCD horizontal size.
        nrows: Number of grid rows.
        ncols: Number of grid columns.
        
    Returns:
        Float representing sum of nearest neighbor distances.
    """
    
    ## Fit parameters
    dy, dx, theta, y0, x0 = params
    
    ## Create grid model and construct x/y coordinate arrays
    grid = BaseGrid(dy, dx, theta, y0, x0)    
    grid_y, grid_x = grid.make_grid(nrows=nrows, ncols=ncols)    
    src_y, src_x = remove_nan_coords(src_y, src_x)   

    ## Calculate distances from each grid model source to nearest true source    
    indices, distances = coordinate_distances(grid_y, grid_x, src_y, src_x)
    nn_distances = distances[:, 0]
    
    return nn_distances.median()

def main(catalog_list, num_processes, output_dir='./', max_displacement=10.0):

    spot_fitter = SpotFitter(max_displacement=max_displacement, output_dir=output_dir)
    
    num_processes = max(1, min(num_processes, mp.cpu_count()-1))
    pool = mp.Pool(num_processes)
    pool.map(spot_fitter.run, catalog_list)
        
if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('catalog_list', nargs='+', 
                        help='Source catalog files to analyze.')
    parser.add_argument('-m', '--max_displacement', type=float, default=10.0, 
                        help='Maximum allowed displacement threshold.')
    parser.add_argument('-n', '--num_processes', type=int, default=1,
                        help='Number of parallel processes to spawn')
    parser.add_argument('--output_dir', '-o', type=str, default='./',
                        help='Output directory to write result files to.')
    args = parser.parse_args()

    catalog_list = args.catalog_list
    output_dir = args.output_dir
    max_displacement = args.max_displacement
    num_processes = args.num_processes

    main(catalog_list, num_processes=num_processes, 
         output_dir=output_dir, max_displacement=max_displacement)