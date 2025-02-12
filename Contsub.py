import numpy as np
import pandas as pd
import datetime
import os
from astropy.io import fits
import multiprocessing
from multiprocessing.pool import ThreadPool as Pool
from itertools import repeat
from scipy import optimize,stats
from scipy.interpolate import splev, splrep
from scipy.signal import convolve
from scipy import ndimage
import sys
import logging
from abc import ABC, abstractmethod


# create logger
log = logging.getLogger('Contsub')
log.setLevel(logging.DEBUG)

# create console handler and set level to debug
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

# create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# add formatter to ch
ch.setFormatter(formatter)

# add ch to logger
log.addHandler(ch)

class alreadyOpen(Exception):
    pass

class alreadyClosed(Exception):
    pass

class noBeamTable(Exception):
    pass

class tableExists(Exception):
    pass

class tableDimMismatch(Exception):
    pass

class CubeDimIsSmall(Exception):
    pass

class FitFunc(ABC):
    """
    abstract class for writing fitting functions
    """
    def __init__(self):
        pass
    
    @abstractmethod
    def prepare(self, x, data, mask, weight):
        pass
    
    @abstractmethod
    def fit(self, x, data, mask, weight):
        pass
    
class FitBSpline(FitFunc):
    """
    BSpline fitting function based on `splev`, `splrep` in `scipy.interpolate` 
    """
    def __init__(self, order, velWidth, randomState, seq):
        """
        needs to know the order of the spline and the number of knots
        """
        self._order = order
        self._velwid = velWidth
        rs = np.random.SeedSequence(entropy = randomState, spawn_key = (seq,))
        self.rng = np.random.default_rng(rs)
        
    def prepare(self, x, data = None, mask = None, weight = None):
        msort = np.argpartition(x, -2)
        m1l, m2l = msort[-2:]
        m1h, m2h = msort[:2]
        if np.abs(m1l - m2l) == 1 and np.abs(m1h - m2h) == 1:
            dvl = np.abs(x[m1l]-x[m2l])/np.mean([x[m1l],x[m2l]])*3e5
            dvh = np.abs(x[m1h]-x[m2h])/np.mean([x[m1h],x[m2h]])*3e5
            dv = (dvl+dvh)/2
            self._imax = int(len(x)/(self._velwid//dv))+1
            print('len(x) = {}, dv = {}, {}km/s in chans: {}, max order spline = {}'.format(len(x), dv, self._velwid, self._velwid//dv, self._imax))
        else:
            log.debug('probably x values are not changing monotonically, aborting')
            sys.exit(1)
            
        knotind = np.linspace(0, len(x), self._imax, dtype = int)[1:-1]
        chwid = (len(x)//self._imax)//8
        self._knots = lambda: self.rng.integers(-chwid, chwid, size = knotind.shape)+knotind
    
    def fit(self, x, data, mask, weight):
        """
        returns the spline fit and the residuals from the fit
        
        x : x values for the fit
        y : values to be fit by spline
        mask : a mask
        weight : weights for fitting the Spline (not implemented), using mask as weight
        """
        inds = self._knots()
        # log.info(f'inds: {inds}')
        splCfs = splrep(x, data, task = -1, w = mask, t = x[inds], k = self._order)
        spl = splev(x, splCfs)
        return spl, data-spl

class fitMedFilter(FitFunc):
    """
    Median filtering class for continuum subtraction 
    """
    def __init__(self, velWidth):
        """
        needs to know the order of the spline and the number of knots
        """
        self._velwid = velWidth
        
    def prepare(self, x, data = None, mask = None, weight = None):
        msort = np.argpartition(x, -2)
        m1l, m2l = msort[-2:]
        m1h, m2h = msort[:2]
        if np.abs(m1l - m2l) == 1 and np.abs(m1h - m2h) == 1:
            dvl = np.abs(x[m1l]-x[m2l])/np.mean([x[m1l],x[m2l]])*3e5
            dvh = np.abs(x[m1h]-x[m2h])/np.mean([x[m1h],x[m2h]])*3e5
            dv = (dvl+dvh)/2
            self._imax = int(self._velwid//dv)
            if self._imax %2 == 0:
                self._imax += 1
            print('len(x) = {}, dv = {}, {}km/s in chans: {}'.format(len(x), dv, self._velwid, self._velwid//dv))
        else:
            log.debug('probably x values are not changing monotonically, aborting')
            sys.exit(1)
            
    
    def fit(self, x, data, mask, weight):
        """
        returns the median filtered data as line emission
        
        x : x values for the fit
        y : values to be fit
        mask : a mask (not implemented really)
        weight : weights
        """
        cp_data = np.copy(data)
        if not (mask is None):
            data[np.logical_not(mask)] = np.nan
        nandata = np.hstack((np.full(self._imax//2, np.nan), data, np.full(self._imax//2, np.nan)))
        nanMed = np.nanmedian(np.lib.stride_tricks.sliding_window_view(nandata,self._imax), axis = 1)
        # resMed = nanMed[~np.isnan(nanMed)]
        resMed = nanMed
        return resMed, cp_data-resMed


class ContSub():
    """
    a class for performing continuum subtraction on data
    """
    def __init__(self, x, cube, function, mask):
        """
        each object can be initiliazed by passing a data cube, a fitting function, and a mask
        cube : a fits cube containing the data
        function : a fitting function should be built on FitFunc class
        mask : a fitting mask where the pixels that should be used for fitting has a `True` value
        """
        self.cube = cube
        self.function = function
        if mask is None:
            self.mask = None
        else:
            self.mask = np.array(mask, dtype = bool)
        self.x = x
        
    def fitContinuum(self):
        """
        fits the data with the desired function and returns the continuum and the line
        """
        dimy, dimx = self.cube.shape[-2:]
        cont = np.zeros(self.cube.shape)
        line = np.zeros(self.cube.shape)
        self.function.prepare(self.x)
            
        if self.mask is None:
            for i in range(dimx):
                for j in range(dimy):
                    cont[:,j,i], line[:,j,i] = self.function.fit(self.x, self.cube[:,j,i], mask = None, weight = None)
        else:
            for i in range(dimx):
                for j in range(dimy):
                    cont[:,j,i], line[:,j,i] = self.function.fit(self.x, self.cube[:,j,i], mask = self.mask[:,j,i], weight = None)
                
            # log.info(f'row {i} is done')
            
        return cont, line
                
                
class Mask():
    """
    mask class creates a mask using a specific masking method
    """
    def __init__(self, method):
        """
        method should be defined when creating a Mask object
        Method should be built on the ClipMethod class
        """
        self.method = method
        
    def getMask(self, data):
        """
        calculates the mask given the data
        """
        return self.method.createMask(data)
        
class ClipMethod(ABC):
    """
    Abstract class for different methods of making masks
    """
    def __init__(self):
        pass
    
    @abstractmethod
    def createMask(self, data):
        pass
    
class pixSigmaClip(ClipMethod):
    """
    simple sigma clipping class
    """
    def __init__(self, n, sm_kernel = None, dilation = 0, method = 'rms'):
        """
        has to define the multiple of sigma for clipping and the method for calculating the sigma
        
        n : multiple of sigma for clipping
        method : 'rms' or 'mad' for calculating the rms
        """
        self.n = n
        self.dilate = dilation
        if sm_kernel is None:
            self.sm = None
        else:
            sm_kernel = np.array(sm_kernel)
            if len(sm_kernel.shape) == 1:
                self.sm = sm_kernel[:, None, None]
            else:
                self.sm = sm_kernel
        if method == 'rms':
            self.function = self.__rms()
        elif methos == 'mad':
            self.function = self.__mad()
        
    def createMask(self, data):
        """
        calculate a mask from the given data 
        """
        sm_data = self.__smooth(data)
        sigma = self.function(sm_data)
        mask = np.abs(sm_data) < self.n*sigma
        
        struct_dil = ndimage.generate_binary_structure(len(data.shape), 1)
        struct_erd = ndimage.generate_binary_structure(len(data.shape), 2)
        
        for i in range(self.dilate):
            mask = ndimage.binary_dilation(mask, structure=struct_dil, border_value=1).astype(mask.dtype)
            
        for i in range(self.dilate+2):
            mask = ndimage.binary_erosion(mask, structure=struct_erd, border_value=1).astype(mask.dtype)
            
        return mask
    
    def __smooth(self, data):
        if self.sm is None:
            return data
        else:
            sm_data = convolve(data, self.sm, mode = 'same')
            return sm_data
    
    def __rms(self):
        return lambda x: np.sqrt(np.nanmean(np.square(x), axis = (0)))
    
    def __mad(self):
        return lambda x: np.nanmedian(np.abs(np.nanmean(x)-x), axis = (0))
        
class chanSigmaClip(ClipMethod):
    """
    simple sigma clipping class
    """
    def __init__(self, n, method = 'rms'):
        """
        has to define the multiple of sigma for clipping and the method for calculating the sigma
        
        n : multiple of sigma for clipping
        method : 'rms' or 'mad' for calculating the rms
        """
        self.n = n
        if method == 'rms':
            self.function = self.__rms()
        elif methos == 'mad':
            self.function = self.__mad()
        
    def createMask(self, data):
        """
        calculate a mask from the given data 
        """
        sigma = self.function(data)[:,None,None]
        return np.abs(data) < self.n*sigma
    
    def __rms(self):
        return lambda x: np.sqrt(np.nanmean(np.square(x), axis = (1,2)))
    
    def __mad(self):
        return lambda x: np.nanmedian(np.abs(np.nanmean(x)-x), axis = (1,2))