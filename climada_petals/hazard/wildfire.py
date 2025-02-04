"""
This file is part of CLIMADA.

Copyright (C) 2017 ETH Zurich, CLIMADA contributors listed in AUTHORS.

CLIMADA is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free
Software Foundation, version 3.

CLIMADA is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with CLIMADA. If not, see <https://www.gnu.org/licenses/>.

---

Define WildFire class.
"""

__all__ = ['WildFire']

import logging
import itertools
from dataclasses import dataclass
from datetime import date

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from scipy import sparse
from sklearn.neighbors import BallTree
from sklearn.cluster import DBSCAN
import numba

from climada.hazard.centroids.centr import Centroids
from climada.hazard.base import Hazard
from climada.hazard.tag import Tag as TagHazard
from climada.util.constants import ONE_LAT_KM
import climada.util.dates_times as u_dt
import climada.util.coordinates as u_coord

LOGGER = logging.getLogger(__name__)

HAZ_TYPE = 'WF'
""" Hazard type acronym for Wild Fire, might be changed to WFseason or WFsingle """

class WildFire(Hazard):

    """Contains wild fire events.

    Wildfires comprise the challenge that the definition of an event is unclear.
    Reporting standards vary accross regions and over time. Hence, to have
    consistency, we consider an event as a whole fire season. A fire season
    is defined as a whole year (Jan-Dec in the NHS, Jul-Jun in SHS). This allows
    consistent risk assessment across the globe and over time. Hazard for
    which events refer to a fire season have the tag 'WFseason'.

    In order to perform concrete case studies or calibrate impact functions,
    events can be displayed as single fires. In that case they have the tag
    'WFsingle'.

    Attributes
    ----------
    date_end : array
        integer date corresponding to the proleptic Gregorian ordinal,
        where January 1 of year 1 has ordinal 1 (ordinal format of
        datetime library). Represents last day of a wild fire instance
        where the fire was still active.
    n_fires : array
        number of single fires in a fire season

    """

    @dataclass
    class FirmsParams():
        """ DataClass as container for firms parameters.

        Attributes
        ----------
        clean_thresh : int, default = 30
            Minimal confidence value for the data from MODIS instrument
            to be use as input
        days_thres_firms : int, default = 2
            Minimum number of days to consider different fires
        clus_thres_firms : int, default = 15
            Clustering factor which multiplies instrument resolution
        remove_minor_fires_firms : bool, default = True
            removes FIRMS fires below defined theshold of entries
        minor_fire_thres_firms : int, default = 3
            number of FIRMS entries required to be considered a fire

        """
        clean_thresh: int = 30
        days_thres_firms: int = 2
        clus_thres_firms: int = 15
        remove_minor_fires_firms: bool = True
        minor_fire_thres_firms: int = 3

    @dataclass
    class ProbaParams():
        """ DataClass as container for parameters for generation of
        probabilistic events.

        PLEASE BE AWARE: Parameter values did not undergo any calibration.

        Attributes
        ----------
        blurr_steps : int, default = 4
            steps with exponential decay for fire propagation matrix
        prop_proba : float, default = 0.21
        max_it_propa : float, default = 500000
        """
        blurr_steps: int = 4
        prop_proba: float = 0.21
        max_it_propa: int = 500000

    def __init__(self):
        """Empty constructor. """
        Hazard.__init__(self, HAZ_TYPE)
        self.FirmsParams = self.FirmsParams()
        self.ProbaParams = self.ProbaParams()

    def set_hist_fire_FIRMS(self, df_firms, centr_res_factor=1.0, centroids=None):
        """ Parse FIRMS data and generate historical fires by temporal and spatial
        clustering. Single fire events are defined as a set of data points
        that are geographically close and/or have consecutive dates. The
        unique identification is made in two steps. First a temporal
        clustering is applied to cleaned data obtained from FIRMS. Data points
        with acquisition dates more than days_thres_firms days apart are
        in different temporal clusters. Second, for each temporal cluster,
        unique event are identified by performing a spatial clustering. This
        is done iteratively until all firms data points are assigned to an event.

        This method sets the attributes self.n_fires, self.date_end, in
        addition to all attributes required by the hazard class.

        This method creates a centroids raster if centroids=None with
        resolution given by centr_res_factor. The centroids can be retrieved
        from Wildfire.centroids()

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data as pd.Dataframe
            (https://firms.modaps.eosdis.nasa.gov/download/)
        centr_res_factor : float, optional, default=1.0
            resolution factor with respect to the satellite data to use
            for centroids creation. Hence, if MODIS data (1 km res) is
            used and centr_res_factor is set to 0.2, the grid spacing of
            the generated centroids will equal 5 km (=1/0.2). If centroids
            are defined, this parameter has no effect.
        centroids : Centroids, optional
            centroids in degrees to map data, centroids need to be on a
            regular raster grid in order for the clustrering to work.
        """
        self.clear()

        # read and initialize data
        df_firms = self._clean_firms_df(df_firms)
        # compute centroids
        res_data = self._firms_resolution(df_firms)
        if not centroids:
            centroids = self._firms_centroids_creation(df_firms, res_data, centr_res_factor)
        else:
            if not centroids.lat.any():
                centroids.set_meta_to_lat_lon()
        res_centr = self._centroids_resolution(centroids)

        # fire identification
        while df_firms.iter_ev.any():
            # Compute cons_id: consecutive fires in current iteration
            self._firms_cons_days(df_firms)
            # Compute clus_id: cluster identifier inside cons_id
            self._firms_clustering(df_firms, res_data)
            # compute event_id
            self._firms_fire(df_firms)
            LOGGER.info('Remaining fires to identify: %s.', str(np.argwhere(\
            df_firms.iter_ev.values).size))

        # remove minor fires
        if self.FirmsParams.remove_minor_fires_firms:
            df_firms = self._firms_remove_minor_fires(df_firms,
                                    self.FirmsParams.minor_fire_thres_firms)

        # compute brightness and fill class attributes
        LOGGER.info('Computing intensity of %s fires.',
                    np.unique(df_firms.event_id).size)
        self._calc_brightness(df_firms, centroids, res_centr)

    def set_hist_fire_seasons_FIRMS(self, df_firms, centr_res_factor=1.0,
                                    centroids=None, hemisphere=None,
                                    year_start=None, year_end=None,
                                    keep_all_fires=False):

        """ Parse FIRMS data and generate historical fire seasons.

        Individual fires are created using temporal and spatial clustering
        according to the 'set_hist_fire_FIRMS' method. single fires are then
        summarized to seasons using max intensity at each centroid for each year.

        This method sets the attributes self.n_fires, self.date_end, in
        addition to all attributes required by the hazard class.

        This method creates a centroids raster if centroids=None with
        resolution given by centr_res_factor. The centroids can be retrieved
        from Wildfire.centroids()

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data as pd.Dataframe
            (https://firms.modaps.eosdis.nasa.gov/download/)
        centr_res_factor : float, optional, default=1.0
            resolution factor with respect to the satellite data to use
            for centroids creation
        centroids : Centroids, optional
            centroids in degrees to map data, centroids need to be on a
            regular grid in order for the clustrering to work.
        hemisphere : str, optional
            'SHS' or 'NHS' to define fire seasons. The hemisphere parameter
            is only used for the definition of the start of the fire season
        year_start : int, optional
            start year; FIRMS fires before that are cut; no cut if not
            specified
        year_end : int, optional
            end year; FIRMS fires after that are cut; no cut if not
            specified
        keep_all_fires : bool, optional
            keep list of all individual fires; default is False to save
            memory. If set to true, fires are stored in self.hist_fire_seasons
        """

        LOGGER.info('Setting up historical fires for year set.')
        self.clear()

        # read and initialize data
        df_firms = self._clean_firms_df(df_firms)
        # compute centroids
        res_data = self._firms_resolution(df_firms)
        if not centroids:
            centroids = self._firms_centroids_creation(df_firms, res_data, centr_res_factor)
        else:
            if not centroids.coord.size:
                centroids.set_meta_to_lat_lon()

        # define hemisphere
        if hemisphere is None:
            if centroids.lat[0] > 0:
                hemisphere = 'NHS'
            elif centroids.lat[0] < 0:
                hemisphere = 'SHS'
        if not all(i >= 0 for i in centroids.lat) or \
            all(i <= 0 for i in centroids.lat):
            LOGGER.warning('Not all centroids are on the same hemisphere. \
                        Hemisphere is set to: %s.', hemisphere)

        # define years
        year_i  = year_start if year_start is not None else \
            date.fromordinal(df_firms.datenum.min()).year
        year_e  = year_end if year_end is not None else \
            date.fromordinal(df_firms.datenum.max()).year
        years = np.arange(year_i, year_e+1)

        # make fire seasons
        hist_fire_seasons = [] # list to save fire seasons

        for year in years:
            LOGGER.info('Setting up historical fire seasons %s.', str(year))
            firms_temp = self._select_fire_season(df_firms, year, hemisphere=hemisphere)
            # calculate historic fire seasons
            wf_year = WildFire()
            wf_year.set_hist_fire_FIRMS(firms_temp, centroids=centroids)
            hist_fire_seasons.append(wf_year)

        # fires season (used to define distribution of n fire for the
        # probabilistic fire seasons)
        n_fires = np.zeros(len(years))
        for idx, wf in enumerate(hist_fire_seasons):
            n_fires[idx] = len(wf.event_id)

        if keep_all_fires:
            self.hist_fire_seasons = hist_fire_seasons

        # save
        self.tag = TagHazard('WFseason')
        self.centroids = centroids
        self.n_fires = n_fires
        self.units = 'K' # Kelvin brightness

        # Following values are defined for each fire
        self.event_id = np.arange(1, len(years)+1).astype(int)
        self.event_name = list(map(str, years))
        self.date = np.zeros(len(years), int)
        self.date_end = np.zeros(len(years), int)
        if hemisphere == 'NHS':
            for y_idx, year in enumerate(years):
                self.date[y_idx] = date.toordinal(date(year, 1, 1))
                self.date_end[y_idx] = date.toordinal(date(year, 12, 31))
        elif hemisphere == 'SHS':
            for y_idx, year in enumerate(years):
                self.date[y_idx] = date.toordinal(date(year, 7, 1))
                self.date_end[y_idx] = date.toordinal(date(year+1, 6, 30))
        self.orig = np.ones(len(years), bool)
        self._set_frequency()

        # Following values are defined for each fire and centroid
        self.intensity = sparse.lil_matrix(np.zeros((len(years), len(centroids.lat))))
        for idx, wf in enumerate(hist_fire_seasons):
            self.intensity[idx] = wf.intensity.max(axis=0)
        self.intensity = self.intensity.tocsr()
        self.fraction = self.intensity.copy()
        self.fraction.data.fill(1.0)

    def set_proba_fire_seasons(self, n_fire_seasons=1, n_ignitions=None,
                               keep_all_fires=False):
        """ Generate probabilistic fire seasons.

        Fire seasons are created by running n probabilistic fires per year
        which are then summarized into a probabilistic fire season by
        calculating the max intensity at each centroid for each probabilistic
        fire season.
        Probabilistic fires are created using the logic described in the
        method '_run_one_bushfire'.

        The fire propagation matrix can be assigned separately, if that is not
        done it will be generated on the available historic fire (seasons).

        Intensities are drawn randomly from historic events. Thus, this method
        requires at least one fire to draw from.

        This method modifies self (climada.hazard.WildFire instance)
        by adding probabilistic wildfire seasons.

        Parameters
        ----------
        self : climada.Hazard.WildFire
            must have calculated historic fire seasons before
        n_fire_seasons : int, optional
            number of fire seasons to be generated
        n_ignitions : array, optional
            [min, max]: min/max of uniform distribution to sample from,
            in order to determin n_fire per probabilistic year set.
            If none, min/max is taken from hist.
        keep_all_fires : bool, optional
            keep detailed list of all fires; default is False to save
            memory.
        """
        # min/max for uniform distribtion to sample for n_fires per year
        if n_ignitions is None:
            ign_min = np.min(self.n_fires)
            ign_max = np.max(self.n_fires)
        else:
            ign_min = n_ignitions[0]
            ign_max = n_ignitions[1]

        prob_fire_seasons = [] # list to save probabilistic fire seasons
        # create probabilistic fire seasons
        for i in range(n_fire_seasons):
            n_ign = np.random.randint(ign_min, ign_max)
            LOGGER.info('Setting up probabilistic fire season with %s fires.',\
                        str(n_ign))
            prob_fire_seasons.append(self._set_one_proba_fire_season(n_ign, seed=i))

        if keep_all_fires:
            self.prob_fire_seasons = prob_fire_seasons

        # save
        # Following values are defined for each fire
        new_event_id = np.arange(np.max(self.event_id)+1, np.max(self.event_id)+n_fire_seasons+1)
        self.event_id = np.concatenate((self.event_id, new_event_id), axis=None)
        new_event_name = list(map(str, new_event_id))
        self.event_name = np.append(self.event_name, new_event_name)
        new_orig = np.zeros(len(new_event_id), bool)
        self.orig = np.concatenate((self.orig, new_orig))
        self._set_frequency()

        # Following values are defined for each event and centroid
        new_intensity = sparse.lil_matrix((np.zeros([n_fire_seasons, len(self.centroids.lat)])))
        for idx, wf in enumerate(prob_fire_seasons):
            new_intensity[idx] = sparse.csr_matrix(wf).max(0)
        new_intensity = new_intensity.tocsr()
        self.intensity = sparse.vstack([self.intensity, new_intensity],
                                       format='csr')
        self.fraction = self.intensity.copy()
        self.fraction.data.fill(1.0)

    def combine_fires(self, event_id_merge=None, remove_rest=False,
                      probabilistic=False):
        """ Combine events that are identified as different fire to one event

        Orig fires are removed and a new fire id created; max intensity at
        overlapping centroids is assigned.

        This method modifies self (climada.hazard.WildFire instance) by
        combining single fires.

        Parameters
        ----------
        event_id_merge : array of int, optional
            events to be merged
        remove_rest : bool, optional
            if set to true, only the merged event is returned.
        probabilistic : bool, optional
            differentiate, because probabilistic events have no date.
        """

        if probabilistic is False:
            if event_id_merge is not None:
                # get index of events to merge
                evt_idx_merge = []
                for i in event_id_merge:
                    evt_idx_merge.append(np.argwhere(self.event_id == i).reshape(-1)[0])
                # get dates
                date_start = np.min(self.date[evt_idx_merge])
                date_end = np.max(self.date_end[evt_idx_merge])

                if remove_rest:
                    self.intensity = sparse.csr_matrix( \
                        np.amax(self.intensity[evt_idx_merge], 0))
                    self.event_id = np.array([np.max(self.event_id)+1])
                    self.event_name = list(map(str, self.event_id))
                    self.date = np.array([date_start])
                    self.date_end = np.array([date_end])
                    self.orig = np.ones(1, bool)
                    self._set_frequency()
                    self.fraction = self.intensity.copy()
                    self.fraction.data.fill(1.0)
                else:
                    # merge event & append
                    self.intensity = sparse.vstack([self.intensity, \
                        np.amax(self.intensity[evt_idx_merge], 0)], format='csr')
                    self.event_id = np.append(self.event_id, np.max(self.event_id)+1)
                    self.event_name = list(map(str, self.event_id))
                    self.date = np.append(self.date, date_start)
                    self.date_end = np.append(self.date_end, date_end)
                    self.orig = np.append(self.orig, np.ones(1, bool))

                    # remove initial events
                    self.intensity = self.intensity[np.delete(np.arange(0, \
                        self.intensity.get_shape()[0]), evt_idx_merge)]
                    self.event_id = np.delete(self.event_id, evt_idx_merge)
                    self.event_name = list(map(str, self.event_id))
                    self.date = np.delete(self.date, evt_idx_merge)
                    self.date_end = np.delete(self.date_end, evt_idx_merge)
                    self.orig = np.delete(self.orig, evt_idx_merge)
                    self._set_frequency()
                    self.fraction = self.intensity.copy()
                    self.fraction.data.fill(1.0)

            else:
                self.intensity = sparse.csr_matrix(np.amax(self.intensity, 0))
                self.event_id = np.array([np.max(self.event_id)+1])
                self.event_name = list(map(str, self.event_id))
                self.date = np.array([np.min(self.date)])
                self.date_end = np.array([np.max(self.date_end)])
                self.orig = np.ones(1, bool)
                self._set_frequency()
                self.fraction = self.intensity.copy()
                self.fraction.data.fill(1.0)
            LOGGER.info('The merged event has event_id %s', self.event_id[-1])

        else:
            self.intensity = sparse.csr_matrix(np.amax(self.intensity, 0))
            self.event_id = np.array([np.max(self.event_id)+1])
            self.orig = np.zeros(1, bool)
            self._set_frequency()
            self.fraction = self.intensity.copy()
            self.fraction.data.fill(1.0)

    def summarize_fires_to_seasons(self, year_start=None, year_end=None,
                                   hemisphere=None):
        """ Summarize historic fires into fire seasons.

        Fires are summarized by taking the max intensity at each grid point.

        This method modifies self (climada.hazard.WildFire instance) by
        summarizing individual fires into seasons.

        Parameters
        ----------
        year_start : int, optional
            start year; fires before that are cut; no cut if not specified
        year_end : int, optional
            end year; fires after that are cut; no cut if not specified
        hemisphere : str, optional
            'SHS' or 'NHS' to define fire seasons
        """

        # define hemisphere
        if hemisphere is None:
            if self.centroids.lat[0] > 0:
                hemisphere = 'NHS'
            elif self.centroids.lat[0] < 0:
                hemisphere = 'SHS'
        if not all(i >= 0 for i in self.centroids.lat) or \
            all(i <= 0 for i in self.centroids.lat):
            LOGGER.warning('Not all centroids are on the same hemisphere. \
                        Hemisphere is set to: %s.', hemisphere)

        # define years
        fire_years = np.array([date.fromordinal(i).year for i in self.date])
        years = np.arange(np.min(fire_years), np.max(fire_years)+1)
        if year_start is not None:
            years = np.delete(years, np.argwhere(years < year_start))
        if year_end is not None:
            years = np.delete(years, np.argwhere(years > year_end))

        # summarize to fire season
        date_new = np.zeros(len(years), int)
        date_end_new = np.zeros(len(years), int)
        n_fires = np.zeros(len(years), int)
        intensity_new = sparse.lil_matrix(np.zeros((len(years), len(self.centroids.lat))))

        for i, year in enumerate(years):
            if hemisphere == 'NHS':
                start = date.toordinal(date(year, 1, 1))
                end = date.toordinal(date(year+1, 1, 1))
            elif hemisphere == 'SHS':
                start = date.toordinal(date(year, 7, 1))
                end = date.toordinal(date(year+1, 7, 1))

            date_new[i] = start
            date_end_new[i] = end
            idx = np.where((self.date > start-1) & \
                           (self.date < end + 1))
            n_fires[i] = len(idx)
            intensity_new[i] = sparse.csr_matrix( \
                                    np.amax(self.intensity[idx], 0))

        # save
        self.tag = TagHazard('WFseason')
        self.units = 'K' # Kelvin brightness

        # Following values are defined for each fire season
        self.event_id = np.arange(1, len(years)+1).astype(int)
        self.event_name = list(map(str, years))
        self.date = date_new
        self.date_end = date_end_new
        self.n_fires = n_fires
        self.orig = np.ones(len(years), bool)
        self._set_frequency()

        # Following values are defined for each fire and centroid
        self.intensity = intensity_new.tocsr()
        self.fraction = self.intensity.copy()
        self.fraction.data.fill(1.0)


    #@staticmethod
    def _clean_firms_df(self, df_firms):
        """Read and remove low confidence data from firms:
            - MODIS: remove data where confidence values are lower than clean_thresh
            - VIIRS: remove data where confidence values are set to low (keep
                nominal and high values)

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data as pd.Dataframe

        Returns
        -------
        df_firms : pd.DataFrame
        """
        # Check for the type of instrument (MODIS vs VIIRS)
        # Remove data with low confidence interval
        # Uniformize the name of the birghtness columns between VIIRS and MODIS
        if 'instrument' in df_firms.columns:
            if (df_firms.instrument == 'MODIS').any() or (df_firms.instrument == 'VIIRS').any():
                df_firms_modis = df_firms.drop(df_firms[df_firms.instrument == 'VIIRS'].index)
                df_firms_modis.confidence = np.array(
                    list(map(int, df_firms_modis.confidence.values.tolist())))
                df_firms_modis = df_firms_modis.drop(df_firms_modis[ \
                    df_firms_modis.confidence < self.FirmsParams.clean_thresh].index)
                temp = df_firms_modis
                df_firms_viirs = df_firms.drop(df_firms[df_firms.instrument == 'MODIS'].index)
                if df_firms_viirs.size:
                    df_firms_viirs = df_firms_viirs.drop(df_firms_viirs[ \
                        df_firms_viirs.confidence == 'l'].index)
                    df_firms_viirs = df_firms_viirs.rename(columns={'bright_ti4':'brightness'})
                    temp = temp.append(df_firms_viirs, sort=True)
                    temp = temp.drop(columns=['bright_ti4'])

                df_firms = temp
                df_firms = df_firms.reset_index()
                df_firms = df_firms.drop(columns=['index'])

        df_firms['iter_ev'] = np.ones(len(df_firms), bool)
        df_firms['cons_id'] = np.zeros(len(df_firms), int) - 1
        df_firms['event_id'] = np.zeros(len(df_firms), int)
        df_firms['clus_id'] = np.zeros(len(df_firms), int) - 1
        df_firms['datenum'] = np.array(u_dt.str_to_date(df_firms['acq_date'].values))
        return df_firms

    @staticmethod
    def _firms_resolution(df_firms):
        """ Returns resolution of satellite used in FIRMS in degrees

        Parameters
        ----------
        csv_firms : pd.DataFrame or str
            path to csv file of FIRMS data or FIRMS data as pd.Dataframe

        Returns
        -------
        res_data/ONE_LAT_KM : float
            resolution in degrees
        """
        # Resolution in km of the centroids depends on the data origin.
        if 'instrument' in df_firms.columns:
            if (df_firms['instrument'] == 'MODIS').any():
                res_data = 1.0
            else:
                res_data = 0.375 # For VIIRS data
        return res_data/ONE_LAT_KM

    @staticmethod
    def _firms_centroids_creation(df_firms, res_data, centr_res_factor):
        """ Create centroids according to the extent of the firms data.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data
        res_data : float
            FIRMS instrument resolution in degrees
        centr_res_factor : float
            the factor applied to voluntarly decrease/increase the
            centroids resolution. Hence, in order to set hazard resolution
            of MODIS data to 4 km, a centr_res_factor of 0.25 is applied.

        Returns
        -------
        centroids : Centroids
        """
        centroids = Centroids()
        centroids.set_raster_from_pnt_bounds((df_firms['longitude'].min(), \
            df_firms['latitude'].min(), df_firms['longitude'].max(), \
            df_firms['latitude'].max()), res=res_data/centr_res_factor)
        centroids.set_meta_to_lat_lon()
        centroids.set_area_approx()
        centroids.set_on_land()
        centroids.empty_geometry_points()

        return centroids

    @staticmethod
    def _centroids_resolution(centroids):
        """ Return resolution of the scalar grid of the centroids

        Parameters
        ----------
        centroids (Centroids): centroids instance

        Returns
        -------
        res_centr : float
            grid resolution of centroids
        """
        if centroids.meta:
            res_centr = abs(centroids.meta['transform'][4]), \
                centroids.meta['transform'][0]
        else:
            res_centr, _ = u_coord.get_resolution(centroids.lat, centroids.lon)
        if abs(abs(res_centr[0]) - abs(res_centr[1])) > 1.0e-6:
            raise ValueError('Centroids are not a regular raster')
        return res_centr[0]

    def _firms_cons_days(self, df_firms):
        """ Compute clusters of consecutive days (temporal clusters).

        An interruption of n days (as defined in FirmsParams) is
        necessary to be set in two different temporal clusters.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data

        Returns
        -------
        df_firms : pd.DataFrame
            FIRMS data including info on temporal cluster per point
        """
        LOGGER.debug('Computing clusters of consecutive days.')
        firms_iter = df_firms[df_firms['iter_ev']][['datenum', 'cons_id', 'event_id']]
        max_cons_id = df_firms.cons_id.max() + 1
        for event_id in np.unique(firms_iter.event_id.values):

            firms_cons = firms_iter[firms_iter.event_id == event_id].reset_index()

            # Order firms dataframe per ascending acq_date order
            firms_cons = firms_cons.sort_values('datenum')
            sort_idx = firms_cons.index
            firms_cons = firms_cons.reset_index()
            firms_cons = firms_cons.drop(columns=['index'])

            # Check if there is more than 1 day interruption between data
            firms_cons.at[0, 'cons_id'] = max_cons_id
            max_cons_id += 1
            for index in range(1, len(firms_cons)):
                if abs((firms_cons.at[index, 'datenum'] - firms_cons.at[index-1, 'datenum'])) \
                >= self.FirmsParams.days_thres_firms:
                    firms_cons.at[index, 'cons_id'] = max_cons_id
                    max_cons_id += 1
                else:
                    firms_cons.at[index, 'cons_id'] = firms_cons.at[(index-1), 'cons_id']

            re_order = np.zeros(len(firms_cons), int)
            for data, order in zip(firms_cons.cons_id.values, sort_idx):
                re_order[order] = data
            firms_iter.cons_id.values[firms_iter.event_id == event_id] = re_order

        df_firms.cons_id.values[df_firms['iter_ev'].values] = firms_iter.cons_id.values
        return df_firms

    def _firms_clustering(self, df_firms, res_data):
        """ Compute geographic clusters and sort firms with ascending clus_id
        for each cons_id. Geographic clusters are identified using sci-kit
        learn's DBSCAN algorithm, which finds core samples of high density
        and expands clusters from them.

        https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data
        res_data : float
            FIRMS instrument resolution in degrees
        clus_thres : int
            Clustering factor which multiplies instrument resolution

        Returns
        -------
        df_firms : pd.DataFrame
            FIRMS data including info on spacial cluster per point
        """

        LOGGER.debug('Computing geographic clusters in consecutive fires.')
        firms_iter = df_firms[df_firms['iter_ev']][['latitude', 'longitude', 'cons_id',
                                              'clus_id', 'event_id']]

        for event_id in np.unique(firms_iter.event_id.values):

            firms_cons = firms_iter[firms_iter.event_id == event_id]

            # Creation of an identifier for geographical clustering
            # For each temporal cluster, perform geographical clustering with DBSCAN algo
            for cons_id in np.unique(firms_cons['cons_id'].values):
                temp = np.argwhere(firms_cons['cons_id'].values == cons_id).reshape(-1,)
                lat_lon = firms_cons.iloc[temp][['latitude', 'longitude']].values
                lat_lon_uni, lat_lon_cpy = np.unique(lat_lon, return_inverse=True, axis=0)
                cluster_id = DBSCAN(eps=res_data * self.FirmsParams.clus_thres_firms,
                                    min_samples=1).\
                                    fit(lat_lon_uni).labels_
                cluster_id = cluster_id[lat_lon_cpy]
                firms_cons.clus_id.values[temp] = cluster_id
                firms_iter.clus_id.values[firms_iter.event_id == event_id] = \
                    firms_cons.clus_id.values

        df_firms.clus_id.values[df_firms['iter_ev'].values] = firms_iter.clus_id.values

        return df_firms

    def _firms_fire(self, df_firms):
        """ Creation of event_id for each dataset point.
        A fire is characterized by a unique combination of 'cons_id' and 'clus_id'.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data including info on temporal and spatial cluster per point

        Returns
        -------
        df_firms : pd.DataFrame
            FIRMS data including info on final cluster (=event) per point
        """
        ev_id = 0
        for cons_id in np.unique(df_firms.cons_id.values):
            firms_cons = df_firms.clus_id.values[df_firms.cons_id.values == cons_id]
            for clus_id in np.unique(firms_cons):
                df_firms.event_id.values[np.logical_and(df_firms.cons_id.values == cons_id, \
                df_firms.clus_id.values == clus_id)] = ev_id
                ev_id += 1

        for ev_id in np.unique(df_firms.event_id.values):
            date_ord = np.sort(df_firms.datenum.values[df_firms.event_id.values == ev_id])
            if (np.diff(date_ord) < self.FirmsParams.days_thres_firms).all():
                df_firms.iter_ev.values[df_firms.event_id.values == ev_id] = False
            else:
                df_firms.iter_ev.values[df_firms.event_id.values == ev_id] = True

    @staticmethod
    def _firms_remove_minor_fires(df_firms, minor_fires_thres):
        """ Remove fires containg fewer FIRMS entries than threshold.
        A fire is characterized by a unique combination of 'cons_id' and
        'clus_id'. This function modifies the df_firms in place.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data
        minor_fires_thres : int
            threshold of FIRMS data points for an event

        Returns
        -------
        df_firms : pd.DataFrame
            FIRMS data excluding minor fire events
        """
        # drop minor fires
        for i in range(np.unique(df_firms.event_id).size):
            if (df_firms.event_id == i).sum() < minor_fires_thres:
                df_firms = df_firms.drop(df_firms[df_firms.event_id == i].index)
        # assign new event IDs
        event_id_new = 1
        for i in np.unique(df_firms.event_id):
            df_firms.event_id[df_firms.event_id == i] = event_id_new
            event_id_new = event_id_new + 1

        df_firms = df_firms.reset_index()

        return df_firms

    def _calc_brightness(self, df_firms, centroids, res_centr):
        """ Compute intensity matrix per fire with the maximum brightness at
        each centroid and all other hazard attributes.

        This method modifies self (climada.hazard.WildFire instance) by
        assigning values to the intensity matrix.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data
        centroids : Centroids
        res_centr : float
            centroids resolution in centroids unit
        """
        uni_ev = np.unique(df_firms['event_id'].values)
        num_ev = uni_ev.size
        num_centr = centroids.size

        # For one fire, if more than one points of firms dataframe have the
        # same coordinates, take the maximum brightness value
        # of these points (maximal damages).
        tree_centr = BallTree(centroids.coord, metric='chebyshev')
        if self.pool:
            chunksize = min(num_ev//self.pool.ncpus, 1000)
            bright_list = self.pool.map(self._brightness_one_fire,
                                        itertools.repeat(df_firms, num_ev),
                                        itertools.repeat(tree_centr, num_ev),
                                        uni_ev, itertools.repeat(res_centr),
                                        itertools.repeat(num_centr),
                                        chunksize=chunksize)
        else:
            bright_list = []
            for ev_id in uni_ev:
                bright_list.append(self._brightness_one_fire(df_firms, tree_centr, \
                ev_id, res_centr, num_centr))

        bright_list, df_firms = self._remove_empty_fires(bright_list, df_firms)

        uni_ev = np.unique(df_firms['event_id'].values)
        num_ev = uni_ev.size

        # save
        self.tag = TagHazard('WFsingle')
        self.centroids = centroids
        self.units = 'K' # Kelvin brightness

        # Following values are defined for each fire
        self.event_id = np.arange(1, num_ev+1).astype(int)
        self.event_name = list(map(str, self.event_id))
        self.date = np.zeros(num_ev, int)
        self.date_end = np.zeros(num_ev, int)
        for ev_idx, ev_id in enumerate(uni_ev):
            self.date[ev_idx] = df_firms[df_firms.event_id == ev_id].datenum.min()
            self.date_end[ev_idx] = df_firms[df_firms.event_id == ev_id].datenum.max()
        self.orig = np.ones(num_ev, bool)
        self._set_frequency()

        # Following values are defined for each fire and centroid
        self.intensity = sparse.lil_matrix(np.zeros((num_ev, num_centr)))
        for idx, ev_bright in enumerate(bright_list):
            self.intensity[idx] = ev_bright
        self.intensity = self.intensity.tocsr()
        self.fraction = self.intensity.copy()
        self.fraction.data.fill(1.0)

    @staticmethod
    def _brightness_one_fire(df_firms, tree_centr, ev_id, res_centr, num_centr):
        """ For a given fire, fill in an intensity np.array with the maximum brightness
        at each centroid.

        Parameters
        ----------
        df_firms : pd.DataFrame
            FIRMS data
        centroids : Centroids
        ev_id : int
            id of the selected event

        Returns
        -------
        brightness_ev : lil_matrix
            maximum brightness at each centroids
        """
        LOGGER.debug('Brightness corresponding to FIRMS event %s.', str(ev_id))
        temp_firms = df_firms.reindex(
            index=(np.argwhere(df_firms['event_id'].values == ev_id).reshape(-1,)),
            columns=['latitude', 'longitude', 'brightness', 'datenum'])

        # Identifies the unique (lat,lon) points of the firms dataframe -> lat_lon_uni
        # Set the same index value for each duplicate (lat,lon) points -> lat_lon_cpy
        lat_lon_uni, lat_lon_cpy = np.unique(temp_firms[['latitude', 'longitude']].values,
                                             return_inverse=True, axis=0)
        index_uni = np.unique(lat_lon_cpy, axis=0)

        # Search closest centroid for each firms point
        ind, _ = tree_centr.query_radius(lat_lon_uni, r=res_centr/2, count_only=False,
                                         return_distance=True, sort_results=True)
        ind = np.array([ind_i[0] if ind_i.size else -1 for ind_i in ind])

        brightness_ev = _fill_intensity_max(num_centr, ind, index_uni, lat_lon_cpy,
                                            temp_firms['brightness'].values)

        return sparse.lil_matrix(brightness_ev)

    @staticmethod
    def _remove_empty_fires(bright_list, df_firms):
        """ Removes instances which were identified as an eventbut which
        contain no intensity. This happens for events which occur
        outside of the defined centroids.

        Parameters
        ----------
        bright_list : list
            idnividual wild fires
        firms : pd.DataFrame
            FIRMS data

        Returns
        -------
        bright_list_nonzero : list
            list with events that occured on the defined centroids
        firms : pd.DataFrame
            FIRMS data (with data that occured on the defined centroids)
        """
        bright_list_nonzero = []
        event_id_new = 1
        for i, br_list in enumerate(bright_list):
            if br_list.count_nonzero() > 0:
                bright_list_nonzero.append(br_list)
                df_firms.event_id.values[df_firms.event_id == i+1] = event_id_new
                event_id_new = event_id_new + 1
            else:
                df_firms = df_firms.drop(df_firms[df_firms.event_id == i].index)

        df_firms = df_firms.reset_index()
        LOGGER.info('Returning %s fires that impacted the defined centroids.',
                    len(bright_list_nonzero))

        return bright_list_nonzero, df_firms

    def _set_one_proba_fire_season(self, n_ignitions, seed=8):
        """ Generate a probabilistic fire season.

        Parameters
        ----------
        n_ignitions : int
            number of wild fires for the season
        seed : int

        Returns
        -------
        proba_fires : lil_matrix
            probablistic hazard
        """
        np.random.seed(seed)
        proba_fires = sparse.lil_matrix(np.zeros((n_ignitions, self.centroids.size)))
        for i in range(n_ignitions):
            if np.mod(i, 10) == 0:
                LOGGER.info('Created %s fires', str(i))
            centr_burned = self._run_one_fire()
            proba_fires[i, :] = self._set_proba_intensity(centr_burned)

        return proba_fires

    def _run_one_fire(self):
        """ Run one bushfire on a fire propagation probability matrix.
            If the matrix is not defined, it is constructed using past fire
            experience -> a fire can only propagate on centroids that burned
            in the past including a exponentially blurred range around the
            historic fires.
            The ignition point of a fire can be on any centroid, on which
            the propagation probability equals 1. The fire is then propagated
            with a cellular automat.
            If the fire has not stopped burning after a defined number of
            iterations (self.ProbaParams.max_it_propa, default=500'000),
            the propagation is interrupted.

            Propagation rules:
                1. select a burning centroid (at the start there is only one,
                    afterwards select one randomly)
                2. every adjunct centroid to the selected centroid can start
                    burning with a probability of the overall propagation
                    probability (self.ProbaParams.prop_proba) times the
                    centroid specific propagation probability (which is
                    defined on the fire_propa_matrix).
                3. the selected burning centroid becomes an ember centroid
                    which can not start burning again and thus no longer
                    propagate any fire.
            Properties from centr_burned:
                0 = unburned centroid
                1 = burning centroid
                2 = ember centroid
            Stop criteria: the propagation stops when no centroid is burning.
            The initial version of this code was inspired by
            https://scipython.com/blog/the-forest-fire-model/

        Parameters
        ----------
        self : climada.hazard.WildFire instance
            needs to contain information of at least 1 historic wildfire

        Returns
        -------
        centr_burned : np.array
            array indicating which centroids burned
        """
        # set fire propagation matrix if not already defined
        if not hasattr(self.centroids, 'fire_propa_matrix'):
            self._set_fire_propa_matrix()

        # Ignation only at centroids that burned in the past
        pos_centr = np.argwhere(self.centroids.fire_propa_matrix.reshape( \
            len(self.centroids.lat)) == 1)[:, 1]

        LOGGER.debug('Start ignition.')
        # Random selection of ignition centroid
        for _ in range(self.centroids.size):
            centr = np.random.choice(pos_centr)
            centr_ix = int(centr/self.centroids.shape[1])
            centr_iy = centr%self.centroids.shape[1]
            centr_ix = max(0, centr_ix)
            centr_ix = min(self.centroids.shape[0]-1, centr_ix)
            centr_iy = max(0, centr_iy)
            centr_iy = min(self.centroids.shape[1]-1, centr_iy)
            centr = centr_ix*self.centroids.shape[1] + centr_iy
            if 1 <= centr_ix < self.centroids.shape[0] - 1 and \
            1 <= centr_iy < self.centroids.shape[1] - 1:
                break

        LOGGER.debug('Propagate fire.')
        centr_burned = np.zeros((self.centroids.shape), int)
        centr_burned[centr_ix, centr_iy] = 1
        # Iterate the fire according to the propagation rules
        count_it = 0
        while np.any(centr_burned == 1) and count_it < self.ProbaParams.max_it_propa:
            count_it += 1
            # Select randomly one of the burning centroids
            # and propagate throught its neighborhood
            burned = np.argwhere(centr_burned == 1)
            if len(burned) > 1:
                centr_ix, centr_iy = burned[np.random.randint(0, len(burned))]
            elif len(burned) == 1:
                centr_ix, centr_iy = burned[0]
            if not count_it % (self.ProbaParams.max_it_propa-1):
                LOGGER.warning('Fire propagation not converging at iteration %s.',
                               count_it)
            if 1 <= centr_ix < self.centroids.shape[0]-1 and \
            1 <= centr_iy < self.centroids.shape[1]-1 and \
            self.centroids.on_land[(centr_ix*self.centroids.shape[1] + centr_iy)]:
                centr_burned = self._fire_propagation_on_matrix(self.centroids.shape, \
                    self.centroids.fire_propa_matrix, self.ProbaParams.prop_proba, \
                    centr_ix, centr_iy, centr_burned, np.random.random(500))

        return centr_burned

    @staticmethod
    @numba.njit
    def _fire_propagation_on_matrix(centr_shape, fire_propa_matrix, prop_proba,
                                    centr_ix, centr_iy, centr_burned, prob_array):
        """ Propagation of the fire in the 8 neighbouring cells around
        (centr_ix, centr_iy) according to propagation rules.

        Parameters
        ----------
        centr_shape : np.array
            shape of centroids array
        fire_propa_matrix : np.array
            fire proagation matrix indicating centroid specific fire
            spread probability
        prop_proba : float
            global propagation probability
        centr_ix : int
            x coordinates of the burning centroid in the centroids matrix
        centr_iy : int
            y coordinates of the burning centroid in the centroids matrix
        centr_burned : np.array
            array containing information on burned centroids
        prob_array: np.array
            array of random numbers to draw from for random fire propagation

        Returns
        -------
        centr_burned : np.array
            updated centr_burned matrix
        """
        # Neighbourhood
        hood = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

        # Creation of the temporary centroids grids
        burned_one_step = np.zeros(centr_shape, dtype=numba.int32)
        burned_one_step[centr_ix, centr_iy] = 1

        # Displacements from a centroid to its eight neighbours
        for i_neig, (delta_x, delta_y) in enumerate(hood):
            if prob_array[i_neig] <= prop_proba * \
                fire_propa_matrix[centr_ix+delta_x, centr_iy+delta_y] and \
            centr_burned[centr_ix+delta_x, centr_iy+delta_y] == 0:
                burned_one_step[centr_ix+delta_x, centr_iy+delta_y] = 1

        # Calculate burned area
        centr_burned += burned_one_step

        return centr_burned

    def _set_proba_intensity(self, centr_burned):
        """ The intensity values are chosen randomly at every burned centroid
        from the intensity values of the historical fire

        Parameters
        ----------
        self : climada.hazard.WildFire instance
        centr_burned : np.array
            array indicating which centroids burned

        Returns
        -------
        proba_intensity : lil_matrix
            hazard intensity matrix of generated probabilistic fire
        """
        # The brightness values are chosen randomly at every burned centroids
        # from the brightness values of the historical fire
        ev_proba_uni = centr_burned.nonzero()[0] * self.centroids.shape[1] + \
            centr_burned.nonzero()[1]
        proba_intensity = sparse.lil_matrix(np.zeros((1, self.centroids.size)))
        for ev_prob in ev_proba_uni:
            proba_intensity[0, ev_prob] = np.random.choice( \
                self.intensity.data)

        return proba_intensity

    def _set_fire_propa_matrix(self):

        """ sets fire propagation matrix which is used to propagate
        probabilistic fires. The matrix is set so that burn probability on
        centroids which burned historically is set to 1. A blurr with
        exponential decay of burn probabilities is set around these
        centroids. The blurr width is defined within self.ProbaParams

        Alternatively, the fire propagation probability matrix can be any
        matrix that coresponds to the shape of the centroids and thus not have
        to be set this way.

        This method modifies self (climada.hazard.WildFire instance) by
        populating self.centroids.fire_propa_matrix as np.array

        Parameters
        ----------
        self : climada.hazard.WildFire instance
        """
        # historically burned centroids
        hist_burned = np.zeros(self.centroids.lat.shape, dtype=bool)
        hist_burned = self.intensity.sum(0) > 0.
        self.centroids.hist_burned = hist_burned

        fire_propa_matrix = hist_burned.reshape(self.centroids.shape).astype(float)
        blurr_lvl = np.ones(self.ProbaParams.blurr_steps)
        # exponential decay of fire propagation on cell level
        for i in range(self.ProbaParams.blurr_steps):
            blurr_lvl[i] = 2.**(-i)
        # Neighbourhood
        hood = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

        # loop over matrix to create blurr around historical fires
        for blurr in range(self.ProbaParams.blurr_steps-1):
            for i in range(fire_propa_matrix.shape[0]):
                for j in range(fire_propa_matrix.shape[1]):
                    if fire_propa_matrix[i, j] == blurr_lvl[blurr]:
                        for _, (delta_x, delta_y) in enumerate(hood):
                            try:
                                if fire_propa_matrix[i+delta_x, j+delta_y] == 0:
                                    fire_propa_matrix[i+delta_x, j+delta_y] = blurr_lvl[blurr+1]
                            except IndexError:
                                pass

        self.centroids.fire_propa_matrix = fire_propa_matrix

    def plot_fire_prob_matrix(self):
        """ Plots fire propagation probability matrix as contour plot.
        At this point just to check the matrix but could easily be improved to
        normal map.

        Parameters
        ----------
        self : climada.hazard.WildFire instance

        Returns
        -------
        contour plot : plt
            contour plot of fire_propa_matrix
        """

        lon = np.reshape(self.centroids.lon, self.centroids.fire_propa_matrix.shape)
        lat = np.reshape(self.centroids.lat, self.centroids.fire_propa_matrix.shape)
        plt.contourf(lon, lat, self.centroids.fire_propa_matrix)

    @staticmethod
    def _select_fire_season(df_firms, year, hemisphere='SHS'):
        """ Selects data to create historic fire season. Need to
        differentiate between Northern & Souther hemisphere

        Parameters
        ----------
        firms : pd.DataFrame
            FIRMS data
        year : int
        hemisphere : str, optional
            'NHS' or 'SHS'

        Returns
        -------
        firms : pd.DataFrame
            FIRMS data for specified fire season
        """

        df_firms['date'] = df_firms['acq_date'].apply(pd.to_datetime)
        if hemisphere == 'NHS':
            start = pd.Timestamp(year, 1, 1)
            end = pd.Timestamp(year+1, 1, 1)
        elif hemisphere == 'SHS':
            start = pd.Timestamp(year, 7, 1)
            end = pd.Timestamp(year+1, 7, 1)

        df_firms = df_firms[(df_firms['date'] > start) & (df_firms['date'] < end)]
        df_firms = df_firms.drop('date', axis=1)
        return df_firms

    def _set_frequency(self):
        """Set hazard frequency from intensity matrix.

        Parameters
        ----------
        self : climada.hazard.WildFire instance

        Returns
        -------
        self.frequency : np.array
        """
        delta_time = date.fromordinal(int(np.max(self.date))).year - \
            date.fromordinal(int(np.min(self.date))).year + 1
        num_orig = self.orig.nonzero()[0].size
        if num_orig > 0:
            ens_size = self.event_id.size / num_orig
        else:
            ens_size = 1
        self.frequency = np.ones(self.event_id.size) / delta_time / ens_size

@numba.njit
def _fill_intensity_max(num_centr, ind, index_uni, lat_lon_cpy, fir_bright):
    """ Assigns maximum intensity value for each centroid. This is required
    as it can happen that several firms data points are mapped on to one
    centroid.

    Parameters
    ----------
    num_centr : int
        number of centroids
    ind : np.array
        index of closest centroid of each firms point
    index_uni : np.array
        unique index of each centroid
    lat_lon_cpy : np.array
        lat /lon information of each firms point
    fir_bright : np.array
        brightness of each firms data point

    Returns
    -------
    brightness_ev : np.array
        maximum brightness at each centroids
    """
    brightness_ev = np.zeros((1, num_centr), dtype=numba.float64)
    for idx in range(index_uni.size):
        if ind[idx] != -1:
            brightness_ev[0, ind[idx]] = max(brightness_ev[0, ind[idx]], \
                         np.max(fir_bright[lat_lon_cpy == index_uni[idx]]))
    return brightness_ev
