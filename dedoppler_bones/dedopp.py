#!/usr/bin/env python

import numpy as np
import astropy.io.fits as pyfits
import sys
import os
from helper_functions import chan_freq
import fits_wrapper
import file_writers
import pyximport
pyximport.install(setup_args={"include_dirs":np.get_include()}, reload_support=True)
import taylor_tree as tt
import logging
logger = logging.getLogger(__name__)

import pdb;# pdb.set_trace()
import cProfile

class max_vals:
    def __init__(self):
        self.maxsnr = None
        self.maxdrift = None
        self.maxsmooth = None
        self.maxid = None
        self.total_n_candi = None

class hist_vals:
    ''' Temporary class that saved the normalized spectrum for all drift rates.'''
    def __init__(self):
        self.histsnr = None
        self.histdrift = None
        self.histid = None

class DedopplerTask:
    """ """
    def __init__(self, fitsfile, max_drift, min_drift=0, snr = 25.0, bw=0, rfiwindow = 2, split_dir='/tmp', obs_info=None, LOFAR=False):
        self.min_drift = min_drift
        self.max_drift = max_drift
        self.snr = snr
        if bw > 1:  #bw is bw_compress_width, for file compression, but it is not in use, since it has not been tested.
            self.bw = bw
        else:
            self.bw = 0
        self.rfiwindow = rfiwindow
        self.split_dir = split_dir
        self.LOFAR = LOFAR
        self.fits_handle = fits_wrapper.FITSHandle(fitsfile, split_dir=self.split_dir)
        if (self.fits_handle is None) or (self.fits_handle.status is False):
            raise IOError("FITS file error, aborting...")
        logger.info(self.fits_handle.get_info())
        logger.info("A new Dedoppler Task instance created!")
        self.obs_info = obs_info
        self.status = True

    def get_info(self):
        info_str = "FITS file: %s\n Split FITS files: %s\n drift rates (min, max): (%f, %f)\n SNR: %f\nbw: %f\n"%(self.fits_handle.filename,self.fits_handle.split_filenames, self.min_drift, self.max_drift,self.snr, self.bw)
        return info_str

    def search(self):
        '''Top level search.
        '''
        logger.debug("Start searching...")
        logger.debug(self.get_info())  # EE should print some info here...

        for target_fits in self.fits_handle.fits_list:
##EE-debuging        for i,target_fits in enumerate(self.fits_handle.fits_list[-13:-10]):
##EE-debuging        for i,target_fits in enumerate(self.fits_handle.fits_list[-8:-6]):
            self.logwriter = file_writers.LogWriter('%s/%s.log'%(self.split_dir.rstrip('/'), target_fits.filename.split('/')[-1].replace('.fits','').replace('.fil','')))
            self.filewriter = file_writers.FileWriter('%s/%s.dat'%(self.split_dir.rstrip('/'), target_fits.filename.split('/')[-1].replace('.fits','').replace('.fil','')))

            self.search_fits(target_fits)
#            cProfile.runctx('self.search_fits(target_fits)',globals(),locals(),filename='profile_feb')

    def search_fits(self, fits_obj):
        '''
        '''
##EE say here which fits file I'm working with...
##EE find out why "get info" doesn't pritn anything.. I think this is not bug, maybe just lack of implementation?
#EE replaced it with filename for now.
        logger.info("Start searching for %s"%fits_obj.filename)
        self.logwriter.info("Start searching for %s"%fits_obj.filename)
        spectra, drift_indexes = fits_obj.load_data(bw_compress_width = self.bw, logwriter=self.logwriter)
        tsteps = fits_obj.tsteps
        tsteps_valid = fits_obj.tsteps_valid
        tdwidth = fits_obj.tdwidth
        fftlen = fits_obj.fftlen
        nframes = tsteps_valid
        shoulder_size = fits_obj.shoulder_size

        if self.LOFAR:
            ##EE This flags 10kHz each edge for LOFAR data. (Assuming 1.497456 Hz resolution)
            median_flag = np.median(spectra)
            spectra[:,:6678] = median_flag/float(tsteps)
            spectra[:,-6678:] = median_flag/float(tsteps)
        else:
            ##EE This flags the edges of the PFF for BL data (with 3Hz res)
            median_flag = np.median(spectra)
            spectra[:,:100000] = median_flag/float(tsteps)
            spectra[:,-100000:] = median_flag/float(tsteps)

        #EE Flagging spikes in time series.
        time_series=spectra.sum(axis=1)
        time_series_median = np.median(time_series)
        mask=(time_series-time_series_median)/time_series.std() > 10   #Flagging spikes > 10 in SNR
        if mask.any():
            spectra[mask,:] = time_series_median/float(fftlen)  # So that the value is not the median in the time_series.

        # allocate array for dedopplering
        # init dedopplering array to zero
        tree_dedoppler = np.zeros(tsteps * tdwidth,dtype=np.float64)# + median_flag

        # allocate array for holding original
        # Allocates array in a fast way (without initialize)
        tree_dedoppler_original = np.empty_like(tree_dedoppler)

        #/* allocate array for negative doppler rates */
        tree_dedoppler_flip = np.empty_like(tree_dedoppler)

        #/* build index mask for in-place tree doppler correction */
        ibrev = np.zeros(tsteps, dtype=np.int32)

        for i in range(0, tsteps):
            ibrev[i] = bitrev(i, int(np.log2(tsteps)))

##EE: why are these values tdwidth and not fftlen?
        max_val = max_vals()
        if max_val.maxsnr == None:
            max_val.maxsnr = np.zeros(tdwidth, dtype=np.float64)
        if max_val.maxdrift == None:
            max_val.maxdrift = np.zeros(tdwidth, dtype=np.float64)
        if max_val.maxsmooth == None:
            max_val.maxsmooth = np.zeros(tdwidth, dtype='uint8')
        if max_val.maxid == None:
            max_val.maxid = np.zeros(tdwidth, dtype='uint32')
        if max_val.total_n_candi == None:
            max_val.total_n_candi = 0

##EE-debuging
        hist_val = hist_vals()
        hist_len = int(np.ceil(2*(self.max_drift-self.min_drift)/fits_obj.drift_rate_resolution))
        if hist_val.histsnr == None:
            hist_val.histsnr = np.zeros((hist_len,tdwidth), dtype=np.float64)
        if hist_val.histdrift == None:
            hist_val.histdrift = np.zeros((hist_len), dtype=np.float64)
        if hist_val.histid == None:
            hist_val.histid = np.zeros(tdwidth, dtype='uint32')

        #EE: Making "shoulders" to avoid "edge effects". Could do further testing.
        specstart = (tsteps*shoulder_size/2)
        specend = tdwidth - (tsteps * shoulder_size)

        #--------------------------------
        #Looping over drift_rate_nblock
        #--------------------------------
        drift_rate_nblock = int(np.floor(self.max_drift / (fits_obj.drift_rate_resolution*tsteps_valid)))

##EE-debuging
        kk = 0

        for drift_block in range(-1*drift_rate_nblock,drift_rate_nblock+1):
            #----------------------------------------------------------------------
            # Negative drift rates search.
            #----------------------------------------------------------------------
            if drift_block <= 0:

                #Populates the dedoppler tree with the spectra
                populate_tree(spectra,tree_dedoppler,nframes,tdwidth,tsteps,fftlen,shoulder_size,roll=drift_block,reverse=1)

                #/* populate original array */
                np.copyto(tree_dedoppler_original, tree_dedoppler)

                #/* populate neg doppler array */
                np.copyto(tree_dedoppler_flip, tree_dedoppler_original)

                #/* Flip matrix across X dimension to search negative doppler drift rates */
                FlipX(tree_dedoppler_flip, tdwidth, tsteps)

                logger.info("Doppler correcting reverse...")
                tt.taylor_flt(tree_dedoppler_flip, tsteps * tdwidth, tsteps)
                logger.info( "done...")

                complete_drift_range = fits_obj.drift_rate_resolution*np.array(range(-1*tsteps_valid*(np.abs(drift_block)+1)+1,-1*tsteps_valid*(np.abs(drift_block))+1))

                for k,drift_rate in enumerate(complete_drift_range[(complete_drift_range<self.min_drift) & (complete_drift_range>=-1*self.max_drift)]):
                    indx  = ibrev[drift_indexes[::-1][(complete_drift_range<self.min_drift) & (complete_drift_range>=-1*self.max_drift)][k]] * tdwidth

                    #/* SEARCH NEGATIVE DRIFT RATES */
                    spectrum = tree_dedoppler_flip[indx: indx + tdwidth]

                    mean_val, stddev = comp_stats(spectrum, tdwidth)

                    #/* normalize */
                    spectrum -= mean_val
                    spectrum /= stddev

                    #Reverse spectrum back
                    spectrum = spectrum[::-1]

##EE maybe wrong use of reverse            n_candi, max_val = candsearch(spectrum, specstart, specend, self.snr, drift_rate, fits_obj.header, fftlen, tdwidth, channel, max_val, 1)
                    n_candi, max_val = candsearch(spectrum, specstart, specend, self.snr, drift_rate, fits_obj.header, fftlen, tdwidth, max_val, 0)
                    info_str = "Found %d candidates at drift rate %15.15f\n"%(n_candi, drift_rate)
                    max_val.total_n_candi += n_candi
                    logger.debug(info_str)
                    self.logwriter.info(info_str)

        ##EE-debuging                    np.save(self.split_dir + '/spectrum_dr%f.npy'%(drift_rate),spectrum)

##EE-debuging                    hist_val.histsnr[kk] = spectrum
##EE-debuging                    hist_val.histdrift[kk] = drift_rate
##EE-debuging                    kk+=1
            #----------------------------------------------------------------------
            # Positive drift rates search.
            #----------------------------------------------------------------------
            if drift_block >= 0:

                #Populates the dedoppler tree with the spectra
                populate_tree(spectra,tree_dedoppler,nframes,tdwidth,tsteps,fftlen,shoulder_size,roll=drift_block,reverse=1)

                #/* populate original array */
                np.copyto(tree_dedoppler_original, tree_dedoppler)

                logger.info("Doppler correcting forward...")
                tt.taylor_flt(tree_dedoppler, tsteps * tdwidth, tsteps)
                logger.info("done...")
                if (tree_dedoppler == tree_dedoppler_original).all():
                     logger.error("taylor_flt has no effect?")
                else:
                     logger.debug("tree_dedoppler changed")

                ##EE: Calculates the range of drift rates for a full drift block.
                complete_drift_range = fits_obj.drift_rate_resolution*np.array(range(tsteps_valid*(drift_block),tsteps_valid*(drift_block +1)))

                for k,drift_rate in enumerate(complete_drift_range[(complete_drift_range>=self.min_drift) & (complete_drift_range<=self.max_drift)]):

                    indx  = (ibrev[drift_indexes[k]] * tdwidth)
                    #/* SEARCH POSITIVE DRIFT RATES */
                    spectrum = tree_dedoppler[indx: indx+tdwidth]

                    mean_val, stddev = comp_stats(spectrum, tdwidth)

                    #/* normalize */
                    spectrum -= mean_val
                    spectrum /= stddev

                    n_candi, max_val = candsearch(spectrum, specstart, specend, self.snr, drift_rate, fits_obj.header, fftlen, tdwidth, max_val, 0)
                    info_str = "Found %d candidates at drift rate %15.15f\n"%(n_candi, drift_rate)
                    max_val.total_n_candi += n_candi
                    logger.debug(info_str)
                    self.logwriter.info(info_str)

        ##EE-debuging                    np.save(self.split_dir + '/spectrum_dr%f.npy'%(drift_rate),spectrum)

##EE-debuging                    hist_val.histsnr[kk] = spectrum
##EE-debuging                    hist_val.histdrift[kk] = drift_rate
##EE-debuging                    kk+=1

#----------------------------------------

##EE-debuging        np.save(self.split_dir + '/histsnr.npy', hist_val.histsnr)
##EE-debuging        np.save(self.split_dir + '/histdrift.npy', hist_val.histdrift)

        #----------------------------------------
        # Writing to file the top hits.
        self.filewriter = tophitsearch(tree_dedoppler_original, max_val, tsteps, nframes, fits_obj.header, tdwidth, fftlen, split_dir = self.split_dir, logwriter=self.logwriter, filewriter=self.filewriter, obs_info = self.obs_info)

        logger.info("Total number of candidates for "+ fits_obj.filename +" is: %i"%max_val.total_n_candi)



#  ======================================================================  #

def populate_tree(spectra,tree_dedoppler,nframes,tdwidth,tsteps,fftlen,shoulder_size,roll=0,reverse=0):
    """ This script populates the dedoppler tree with the spectra.
        It creates two "shoulders" (each region of tsteps*(shoulder_size/2) in size) to avoid "edge" issues.
        It uses np.roll() for drift-rate blocks higher than 1.
    """

    if reverse:
        direction = -1
    else:
        direction = 1

#EE Also, the shouldering is maybe not important, since I'm already making my own flagging fo 10k channels
#EE And Since , I have a very large frequency number comparted to the time lenght.
##EE Wondering if here should have a data cube instead...maybe not, i guess this is related to the bit-reversal.

    for i in range(0, nframes):
        sind = tdwidth*i + tsteps*shoulder_size/2
        cplen = fftlen

        ##EE copy spectra into tree_dedoppler, leaving two regions in each side blanck (each region of tsteps*(shoulder_size/2) in size).
#        np.copyto(tree_dedoppler[sind: sind + cplen], spectra[i])
        # Copy spectra into tree_dedoppler, with rolling.
        np.copyto(tree_dedoppler[sind: sind + cplen], np.roll(spectra[i],roll*i*direction))

#EE code below will be replaced, since I think is better to use the median of the data as "flag" than to add other data into the shoulders.

#         ##EE loads the end part of the current spectrum into the left hand side black region in tree_dedoppler (comment below says "next spectra" but for that need i+1...bug?)
         #//load end of current spectra into left hand side of next spectra
        sind = i * tdwidth
        np.copyto(tree_dedoppler[sind: sind + tsteps*shoulder_size/2], spectra[i, fftlen-(tsteps*shoulder_size/2):fftlen])

    return tree_dedoppler

#  ======================================================================  #
#  This function bit-reverses the given value "inval" with the number of   #
#  bits, "nbits".    ----  R. Ramachandran, 10-Nov-97, nfra.               #
#  python version ----  H. Chen   Modified 2014                            #
#  ======================================================================  #
def bitrev(inval, nbits):
    if nbits <= 1:
        ibitr = inval
    else:
        ifact = 1
        for i in range(1, nbits):
           ifact *= 2
        k = inval
        ibitr = (1 & k) * ifact
        for i in range(2, nbits+1):
            k /= 2
            ifact /= 2
            ibitr += (1 & k) * ifact
    return ibitr

#  ======================================================================  #
#  This function bit-reverses the given value "inval" with the number of   #
#  bits, "nbits".                                                          #
#  python version ----  H. Chen   Modified 2014                            #
#  reference: stackoverflow.com/questions/12681945                         #
#  ======================================================================  #
def bitrev2(inval, nbits, width=32):
    b = '{:0{width}b}'.format(inval, width=width)
    ibitr = int(b[-1:(width-1-nbits):-1], 2)
    return ibitr

#  ======================================================================  #
#  This function bit-reverses the given value "inval" with 32bits    #
#  python version ----  E.Enriquez   Modified 2016                            #
#  reference: stackoverflow.com/questions/12681945                         #
#  ======================================================================  #
def bitrev3(x):
    raise DeprecationWarning("WARNING: This needs testing.")

    x = ((x & 0x55555555) << 1) | ((x & 0xAAAAAAAA) >> 1)
    x = ((x & 0x33333333) << 2) | ((x & 0xCCCCCCCC) >> 2)
    x = ((x & 0x0F0F0F0F) << 4) | ((x & 0xF0F0F0F0) >> 4)
    x = ((x & 0x00FF00FF) << 8) | ((x & 0xFF00FF00) >> 8)
    x = ((x & 0x0000FFFF) << 16) | ((x & 0xFFFF0000) >> 16)
    return x

def AxisSwap(inbuf, outbuf, nchans, NTSampInRead):
    #long int    j1, j2, indx, jndx;
    for j1 in range(0, NTSampInRead):
        indx  = j1 * nchans
        for j2 in range(nchans-1, -1, -1):
            jndx = j2 * NTSampInRead + j1
            outbuf[jndx]  = inbuf[indx+j2]

def FlipBand(outbuf, nchans, NTSampInRead):
    temp = np.zeros(nchans*NTSampInRead, dtype=np.float64)

    indx  = (nchans - 1);
    for i in range(0, nchans):
        jndx = (indx - i) * NTSampInRead
        kndx = i * NTSampInRead
        np.copyto(temp[jndx: jndx+NTSampInRead], outbuf[kndx + NTSampInRead])
    #memcpy(outbuf, temp, (sizeof(float)*NTSampInRead * nchans));
    outbuf = temp
    return

def FlipX(outbuf, xdim, ydim):
    temp = np.empty_like(outbuf[0:xdim])
    logger.debug("FlipX: temp array dimension: %s"%str(temp.shape))

    for j in range(0, ydim):
        indx = j * xdim
        np.copyto(temp, outbuf[indx:indx+xdim])
        np.copyto(outbuf[indx: indx+xdim], temp[::-1])
    return

def comp_stats(vec, veclen):
    #Compute mean and stddev of floating point vector vec in a fast way, without using the outliers.

    new_vec = np.empty_like(vec)
    np.copyto(new_vec,vec)
    new_vec.sort()
    #Removing the lowest 20% and highest 10% of data, this takes care of outliers.
    new_vec = vec[int(len(vec)*.2):int(len(vec)*.9)]
    tmedian = np.median(new_vec)
    tstddev = new_vec.std()

    return tmedian, tstddev

def candsearch(spectrum, specstart, specend, candthresh, drift_rate, header, fftlen, tdwidth, max_val, reverse):
    ''' Searches for candidates: each channel if > candthresh.
    '''

    logger.debug('Start searching for drift rate: %f'%drift_rate)
    j = 0
    for i in (spectrum[specstart:specend] > candthresh).nonzero()[0] + specstart:
        k =  (tdwidth - 1 - i) if reverse else i
        info_str = 'Candidate found at SNR %f! %s\t'%(spectrum[i], '(reverse)' if reverse else '')
        info_str += 'Spectrum index: %d, Drift rate: %f\t'%(i, drift_rate)
        info_str += 'Uncorrected frequency: %f\t'%chan_freq(header,  k, tdwidth, 0)
        info_str += 'Corrected frequency: %f'%chan_freq(header, k, tdwidth, 1)
        logger.debug(info_str)
        j += 1
        used_id = j
        if spectrum[i] > max_val.maxsnr[k]:
            max_val.maxsnr[k] = spectrum[i]
            max_val.maxdrift[k] = drift_rate
            max_val.maxid[k] = used_id

    return j, max_val

def tophitsearch(tree_dedoppler_original, max_val, tsteps, nframes, header, tdwidth, fftlen, split_dir='', logwriter=None, filewriter=None,obs_info=None):
    '''This finds the candidate with largest SNR within 2*tsteps frequency channels.
    '''

    maxsnr = max_val.maxsnr
    logger.debug("original matrix size: %d\t(%d, %d)"%(len(tree_dedoppler_original), tsteps, tdwidth))
    tree_orig = tree_dedoppler_original.reshape((tsteps, tdwidth))
    logger.debug("tree_orig shape: %s"%str(tree_orig.shape))

    for i in (maxsnr > 0).nonzero()[0]:
        lbound = max(0, i - tsteps/2)
        ubound = min(tdwidth, i + tsteps/2)
        skip = 0

        if (maxsnr[lbound:ubound] > maxsnr[i]).nonzero()[0].any():
            skip = 1

        if skip:
            logger.debug("SNR not big enough... %f pass... index: %d"%(maxsnr[i], i))
        else:
            info_str = "Top hit found! SNR: %f ... index: %d"%(maxsnr[i], i)
            logger.info(info_str)
            if logwriter:
                logwriter.info(info_str)
                #logwriter.report_tophit(max_val, i, header)
#EE            logger.debug("slice of spectrum...size: %s"%str(tree_orig[0:nframes, lbound:ubound].shape))
            if filewriter:
                filewriter = filewriter.report_tophit(max_val, i, (lbound, ubound), tdwidth, fftlen, header,obs_info=obs_info)
#EE: not passing array cut, since not saving in .dat file                filewriter = filewriter.report_tophit(max_val, i, (lbound, ubound), tree_orig[0:nframes, lbound:ubound], header)

##EE : Uncomment if want to save each blob              np.save(split_dir + '/spec_drift_%.4f_id_%d.npy'%(max_val.maxdrift[i],i), tree_orig[0:nframes, lbound:ubound])
            else:
                logger.error('Not have filewriter? tell me why.')

    return filewriter