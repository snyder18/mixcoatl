import os
import numpy as np
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase

from lsst.eotest.sensor.MaskedCCD import MaskedCCD

from mixcoatl.crosstalk import make_stamp, calibrated_stack, crosstalk_model_fit 
from mixcoatl.crosstalk import CrosstalkMatrix

class CrosstalkConfig(pexConfig.Config):
    
    nsig = pexConfig.Field("Outlier rejection sigma threshold", float, default=5.0)
    num_iter = pexConfig.Field("Number of least square iterations", int, default=3)
    threshold = pexConfig.Field("Aggressor spot mean signal threshold", float,
                                default=40000.)
    output_dir = pexConfig.Field("Output directory", str, default='.')
    output_file = pexConfig.Field("Output filename", str, default=None)
    verbose = pexConfig.Field("Turn verbosity on", bool, default=True)

class CrosstalkTask(pipeBase.Task):

    ConfigClass = CrosstalkConfig
    _DefaultName = "CrosstalkTask"

    @pipeBase.timeMethod
    def run(self, sensor_id1, infiles1, gains1, bias_frame1=None, dark_frame1=None,
            **kwargs):

        ## Parse kwargs for separate victim parameters
        try:
            sensor_id2 = kwargs['sensor_id2']
        except KeyError:
            sensor_id2 = sensor_id1
            infiles2 = infiles1
            gains2 = gains1
            bias_frame2 = bias_frame1
            dark_frame2 = dark_frame1
        else:
            infiles2 = kwargs['infiles2']
            gains2 = kwargs['gains2']
            bias_frame2 = bias_frame2['bias_frame2']
            dark_frame2 = dark_frame2['dark_frame2']
        
        ccds1 = [MaskedCCD(infile, bias_frame=bias_frame1, 
                           dark_frame=dark_frame1) for infile in infiles1]
        ccds2 = [MaskedCCD(infile, bias_frame=bias_frame2,
                           dark_frame=dark_frame2) for infile in infiles2]

        ## Create new matrix or modify existing
        if self.output_file is not None:
            output_file = self.output_file
            crosstalk_matrix = CrosstalkMatrix(sensor_id1, sensor_id2, self.output_file)
        else:
            output_file = os.path.join(self.output_dir, 
                                       '{0}_{1}_crosstalk_matrix.fits'.format(sensor_id1, 
                                                                              sensor_id2))
            crosstalk_matrix = CrosstalkMatrix(sensor_id1, sensor_id2)

        all_amps = imutils.AllAmps(infiles1[0])
        num_aggressors = 0

        ## Search each amp for aggressor spot
        for amp in all_amps:
            imarr1 = calibrated_stack(ccds1, amp, gain=gains1[amp])
            smoothed = gaussian_filter(imarr1, 20)
            y, x = np.unravel_index(smoothed.argmax(), smoothed.shape)
            Y, X = np.ogrid[-y:smoothed.shape[0]-y, -x:smoothed.shape[1]-x]
            mask = X*X + Y*Y >= 20*20

            ## Verify signal threshold
            if np.mean(test) > threshold:
                stamp = make_stamp(imarr1, y, x)
                row = {}

                ## Calculate crosstalk for each victim amp
                for i in range(1, 17):
                    imarr2 = calibrated_stack(ccds2, i, gain=gains2[i])
                    stamp2 = make_stamp(imarr2, y, x)
                    noise = 6.0
                    row[i] = crosstalk_model_fit(stamp1, stamp2, noise)
                crosstalk_matrix.set_row(amp, row)
                if num_aggressors == 4: break

        crosstalk_matrix.write_fits(output_file)
