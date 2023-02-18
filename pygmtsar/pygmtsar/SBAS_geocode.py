#!/usr/bin/env python3
# Alexey Pechnikov, Sep, 2021, https://github.com/mobigroup/gmtsar
from .SBAS_sbas import SBAS_sbas
from .tqdm_dask import tqdm_dask

class SBAS_geocode(SBAS_sbas):

    def geocode_parallel(self, pairs=None):
        # find any one interferogram to build the geocoding matrices
        if pairs is None:
            pairs = self.find_pairs()
        # build trans_dat and topo_ra grids
        self.topo_ra_parallel()
        # define the target interferogram grid
        intf_grid = self.open_grids(pairs, 'phasefilt').min('pair').astype(bool)
        # build geographic coordinates transformation matrix for landmask and other grids
        self.intf_ll2ra_matrix_parallel(intf_grid)
        # build radar coordinates transformation matrix for the interferograms grid stack        
        self.intf_ra2ll_matrix_parallel(intf_grid)

##########################################################################################
# ra2ll
##########################################################################################
    def intf_ra2ll_matrix(self, intf_grid, interactive=False):
        import xarray as xr
        import numpy as np
        import os

        # trans.dat - file generated by llt_grid2rat (r a topo lon lat)"
        trans_dat = self.get_trans_dat()
            # y index on trans_dat lat/lon grid
        trans_yidx = ((trans_dat.azi - intf_grid.y.min())/intf_grid.y.diff('y')[0]).round()
        trans_yidx = xr.where((trans_yidx>=0)&(trans_yidx<intf_grid.y.size), trans_yidx.astype(np.uint32), self.noindex)

        # x index on trans_dat lat/lon grid
        trans_xidx = ((trans_dat.rng - intf_grid.x.min())/intf_grid.x.diff('x')[0]).round()
        trans_xidx = xr.where((trans_xidx>=0)&(trans_xidx<intf_grid.x.size), trans_xidx.astype(np.uint32), self.noindex)

        # matrix of linear index on trans_dat lat/lon grid
        matrix_ll = xr.where((trans_yidx==self.noindex)|(trans_xidx==self.noindex),
                             self.noindex,
                             trans_yidx * intf_grid.x.size + trans_xidx).drop_vars(['y','x']).rename('intf_ra2ll')

        if interactive:
            return matrix_ll

        # save to NetCDF file
        filename = self.get_filenames(None, None, 'intf_ra2ll')
        #print ('filename', filename)
        # to resolve NetCDF rewriting error
        if os.path.exists(filename):
            os.remove(filename)
        # flip vertically for GMTSAR compatibility reasons
        handler = matrix_ll.to_netcdf(filename,
                                    encoding={'intf_ra2ll': self.compression},
                                    engine=self.engine,
                                    compute=False)
        return handler

    def intf_ra2ll_matrix_parallel(self, intf_grid, interactive=False):
        import dask

        delayed = self.intf_ra2ll_matrix(intf_grid, interactive=interactive)

        if not interactive:
            tqdm_dask(dask.persist(delayed), desc='Build ra2ll Transform')
        else:
            return delayed

    # lat,lon grid
    def get_intf_ra2ll(self):
        return self.open_grids(None, 'intf_ra2ll')

    # TODO: split to blocks and apply transform for the each block parallel
    def intf_ra2ll(self, grids):
        """
        Inverse geocoding function based on interferogram geocode matrix to call from open_grids(inverse_geocode=True)
        """
        import xarray as xr
        import numpy as np

        # helper check
        if not 'y' in grids.dims and 'x' in grid.dims:
            print ('NOTE: the grid is not in geograpphic coordinates, miss geocoding')
            return grids

        # unify the input grids for transform matrix defined on the intf grid (y, x)
        source_grid = self.get_intf_ll2ra()
        grids = grids.interp_like(source_grid, method='nearest')

        # target grid is tranform matrix
        matrix_ra2ll = self.get_intf_ra2ll()

        def ra2ll(grid):
            # transform input grid to the trans_ra2ll where the geocoding matrix defined
            # only nearest interpolation allowed to save values of binary masks
            # look for valid indices only
            valid_index = np.where(matrix_ra2ll!=self.noindex, matrix_ra2ll, 0)
            return xr.DataArray(np.where(matrix_ra2ll != self.noindex,
                                         grid.reshape(-1)[valid_index],
                                         np.nan),
                                coords=matrix_ra2ll.coords).expand_dims('new').values

        # xarray wrapper
        grids_ll = xr.apply_ufunc(
            ra2ll,
            (grids.expand_dims('new') if len(grids.dims)==2 else grids).chunk({'y':-1, 'x':-1}),
            input_core_dims=[['y','x']],
            exclude_dims=set(('y','x')),
            dask='parallelized',
            vectorize=False,
            output_dtypes=[np.float32],
            output_core_dims=[['lat','lon']],
            dask_gufunc_kwargs={'output_sizes': {'lat': matrix_ra2ll.shape[0], 'lon': matrix_ra2ll.shape[1]}},
        ).chunk(self.chunksize)

        # set the output grid and drop the fake dimension if needed
        out = xr.DataArray((grids_ll[0] if len(grids.dims)==2 else grids_ll), coords=matrix_ra2ll.coords)
        # append source grid coordinates excluding removed y, x ones
        for (k,v) in grids.coords.items():
            if k not in ['y','x']:
                out[k] = v
        return out

##########################################################################################
# ll2ra
##########################################################################################
    def intf_ll2ra_matrix(self, intf_grid, interactive=False):
        from scipy.spatial import cKDTree
        import dask
        import xarray as xr
        import numpy as np
        import os

        # trans.dat - file generated by llt_grid2rat (r a topo lon lat)"
        trans_dat = self.get_trans_dat()
        trans_blocks_extents = self.get_trans_dat_blocks_extents()

        # target grid - do not use coordinate names y,x, because the output grid saved as (y,y) in this case...
        azis = xr.DataArray(intf_grid.y.values, dims=['a'], coords={'a': intf_grid.y.values}).chunk(-1)
        rngs = xr.DataArray(intf_grid.x.values, dims=['r'], coords={'r': intf_grid.x.values}).chunk(-1)
        azis, rngs = xr.broadcast(azis, rngs)
        # unify chunks to have the same dask blocks - pay attention dimensions are renamed
        _, azis, rngs = xr.unify_chunks(intf_grid.rename({'y': 'a', 'x': 'r'}), azis, rngs)
        assert intf_grid.chunks == azis.chunks and intf_grid.chunks == rngs.chunks, \
                'Chunks are not equal'

        def calc_intf_ll2ra_matrix(azi, rng):
            # check arguments
            assert azi.shape == rng.shape, f'ERROR: {azi.shape} != {rng.shape}'
            # check the selected area bounds
            ymin, ymax, xmin, xmax = azi.min(), azi.max(), rng.min(), rng.max()
            #print ('ymin, ymax', ymin, ymax, 'xmin, xmax', xmin, xmax)
            # define corresponding trans_dat blocks
            ymask = (trans_blocks_extents[:,3]>=ymin-1)&(trans_blocks_extents[:,2]<=ymax+1)
            xmask = (trans_blocks_extents[:,5]>=xmin-1)&(trans_blocks_extents[:,4]<=xmax+1)
            blocks = trans_blocks_extents[ymask&xmask]
            #print ('trans_dat blocks', blocks.astype(int))

            blocks_azis = []
            blocks_rngs = []
            blocks_idxs = []
            for iy, ix in blocks[:,:2].astype(int):
                #print ('iy, ix', iy, ix)
                block_azi = trans_dat.azi.data.blocks[iy, ix].persist()
                block_rng = trans_dat.rng.data.blocks[iy, ix].persist()
                block_idx = trans_dat.idx.data.blocks[iy, ix].persist()

                blocks_azis.append(block_azi.reshape(-1))
                blocks_rngs.append(block_rng.reshape(-1))
                blocks_idxs.append(block_idx.reshape(-1))

            # found the issue for 5 stitched scenes
            if len(blocks_azis) == 0:
                return np.full(azi.shape, self.noindex)

            blocks_azis = np.concatenate(blocks_azis)
            blocks_rngs = np.concatenate(blocks_rngs)
            blocks_idxs = np.concatenate(blocks_idxs)

            # build index tree - dask arrays computes automatically
            source_yxs = np.stack([blocks_azis, blocks_rngs], axis=1)
            tree = cKDTree(source_yxs, compact_nodes=False, balanced_tree=False)

            # query the index tree
            target_yxs = np.stack([azi.reshape(-1), rng.reshape(-1)], axis=1)
            d, inds = tree.query(target_yxs, k = 1, workers=1)
            # compute dask array to prevent ineffective index loockup on it
            matrix = np.asarray(blocks_idxs)[inds].reshape(azi.shape)
            return matrix

        # xarray wrapper
        matrix_ra = xr.apply_ufunc(
            calc_intf_ll2ra_matrix,
            azis.chunk(self.chunksize),
            rngs.chunk(self.chunksize),
            dask='parallelized',
            vectorize=False,
            output_dtypes=[np.uint32],
        ).rename('intf_ll2ra')

        assert intf_grid.chunks == matrix_ra.chunks, \
            'Inverse geocoding matrix chunks are not equal to interferogram chunks'
        if interactive:
            return matrix_ra

        # save to NetCDF file
        filename = self.get_filenames(None, None, 'intf_ll2ra')
        #print ('filename', filename)
        # to resolve NetCDF rewriting error
        if os.path.exists(filename):
            os.remove(filename)
        handler = matrix_ra.to_netcdf(filename,
                                    encoding={'intf_ll2ra': self.compression},
                                    engine=self.engine,
                                    compute=False)
        return handler

    def intf_ll2ra_matrix_parallel(self, intf_grid, interactive=False):
        import dask
        
        delayed = self.intf_ll2ra_matrix(intf_grid, interactive=interactive)

        if not interactive:
            tqdm_dask(dask.persist(delayed), desc='Build ll2ra Transform')
        else:
            return delayed

    # y,x grid
    def get_intf_ll2ra(self):
        return self.open_grids(None, 'intf_ll2ra').rename({'a': 'y', 'r': 'x'})

    # TODO: split to blocks and apply transform for the each block parallel
    def intf_ll2ra(self, grids):
        """
        Inverse geocoding function based on interferogram geocode matrix to call from open_grids(inverse_geocode=True)
        """
        import xarray as xr
        import numpy as np

        # helper check
        if not 'lat' in grids.dims and 'lon' in grid.dims:
            print ('NOTE: the grid is not in geograpphic coordinates, miss geocoding')
            return grids

        # unify the input grids for transform matrix defined on the trans_dat grid (lat, lon)
        source_grid = self.get_trans_dat().ll
        grids = grids.interp_like(source_grid, method='nearest')
        # target grid is tranform matrix
        matrix_ll2ra = self.get_intf_ll2ra()

        def ll2ra(grid):
            # transform input grid to the trans_ra2ll where the geocoding matrix defined
            # only nearest interpolation allowed to save values of binary masks
            # all the indices available by design
            return xr.DataArray(grid.reshape(-1)[matrix_ll2ra],
                                coords=matrix_ll2ra.coords).expand_dims('new').values

        # xarray wrapper
        grids_ra = xr.apply_ufunc(
            ll2ra,
            (grids.expand_dims('new') if len(grids.dims)==2 else grids).chunk({'lat':-1, 'lon':-1}),
            input_core_dims=[['lat','lon']],
            exclude_dims=set(('lat','lon')),
            dask='parallelized',
            vectorize=False,
            output_dtypes=[np.float32],
            output_core_dims=[['y','x']],
            dask_gufunc_kwargs={'output_sizes': {'y': matrix_ll2ra.shape[0], 'x': matrix_ll2ra.shape[1]}},
        ).chunk(self.chunksize)
        
        # set the output grid and drop the fake dimension if needed
        return xr.DataArray((grids_ra[0] if len(grids.dims)==2 else grids_ra), coords=matrix_ll2ra.coords)
