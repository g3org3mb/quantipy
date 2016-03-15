import numpy as np
import pandas as pd
import quantipy as qp

import quantipy.sandbox

from quantipy.core.view import View
from quantipy.core.view_generators.view_mapper import ViewMapper
from quantipy.core.view_generators.view_maps import QuantipyViews

from quantipy.core.helpers.functions import emulate_meta
from quantipy.core.tools.view.logic import (
    has_any, has_all, has_count,
    not_any, not_all, not_count,
    is_lt, is_ne, is_gt,
    is_le, is_eq, is_ge,
    union, intersection, get_logic_index)

from collections import defaultdict
import gzip
import dill
import json
import copy

class Quantity(object):
    """
    The Quantity object is the main Quantipy aggregation engine.

    Consists of a link's data matrix representation and sectional defintion
    of weight vector (wv), x-codes section (xsect) and y-codes section
    (ysect). The instance methods handle creation, retrieval and manipulation
    of the data input matrices and section definitions as well as the majority
    of statistical calculations.
    """
    # -------------------------------------------------
    # Instance initialization
    # -------------------------------------------------
    def __init__(self, link, weight=None, use_meta=True, base_all=False):
        # Collect information on wv, x- and y-section
        self.quantified = True
        self.is_weighted=False
        self._uses_meta = use_meta
        self.d = link.data
        self.ds = link.dataset
        self.base_all = base_all
        self._dataidx = link.data().index
        if self._uses_meta:
            self.meta = link.meta
            if self.meta().values() == [None] * len(self.meta().values()):
                self._uses_meta = False
                self.meta = None
        else:
            self.meta = None
        self.cache = link.cache
        self.f = link.filters
        self.x = link.x
        self.y = link.y
        self.w = weight if weight is not None else '@1'
        self.type = self._get_type()
        if self.type == 'nested':
            self.nest_def = Nest(self.y, self.d, self.meta).nest()
        self._squeezed = False
        self.idx_map = None
        self.xdef = self.ydef = None
        self.matrix = self._get_matrix()
        self.is_empty = self.matrix.sum() == 0
        self.switched = False
        self.factorized = None
        self.result = None
        self.logical_conditions = []
        self.cbase = self.rbase = None
        self.comb_x = self.comb_y = None
        self.miss_x = self.miss_y = None
        self.calc_x = self.calc_y = None
        self._has_x_margin = self._has_y_margin = False


    def __repr__(self):
        if self.result is not None:
            return '%s' % (self.result)
        else:
            info = 'Link - id: {}\nstack connected: {} | views: {}'
            return info.format(self.id, self.stack_connection, len(self.values()))

    # -------------------------------------------------
    # Matrix creation and retrievel
    # -------------------------------------------------
    def _get_type(self):
        """
        Test variable type that can be "simple", "nested" or "array".
        """
        if self._uses_meta:
            if self.x in self.meta()['masks'].keys():
                if self.meta()['masks'][self.x]['type'] == 'array':
                    return 'array'
            elif '>' in self.y:
                return 'nested'
            else:
                return 'simple'
        else:
            return 'simple'

    def _is_multicode_array(self, mask_element):
        return self.d()[mask_element].dtype == 'object'

    def _get_wv(self):
        """
        Returns the weight vector of the matrix.
        """
        return self.d()[[self.w]].values

    def weight(self):
        """
        Weight by multiplying the indicator entries with the weight vector.
        """
        if self.is_weighted:
            self.matrix[:, 1:, 1:] *=  np.atleast_3d(self.wv)
        else:
            self.matrix *= np.atleast_3d(self.wv)
        self.is_weighted = True
        return None

    def unweight(self):
        """
        Remove any weighting by dividing the matrix by itself.
        """
        self.matrix[:, 1:, 1:] /= self.matrix[:, 1:, 1:]
        self.is_weighted = True
        return None

    def _get_total(self):
        """
        Return a vector of 1s for the matrix.
        """
        return self.d()[['@1']].values

    def _copy(self):
        """
        Copy the Quantity instance, i.e. its data matrix, into a new object.
        """
        m_copy = np.empty_like(self.matrix)
        m_copy[:] = self.matrix
        c = copy.copy(self)
        c.matrix = m_copy
        return c

    def _get_response_codes(self, var):
        """
        Query the meta specified codes values for a meta-using Quantity.
        """
        if self.type == 'array':
            rescodes = [v['value'] for v in self.meta()['lib']['values'][var]]
        else:
            values = emulate_meta(
                self.meta(), self.meta()['columns'][var].get('values', None))
            rescodes = [v['value'] for v in values]
        return rescodes

    def _get_response_texts(self, var, text_key=None):
        """
        Query the meta specified text values for a meta-using Quantity.
        """
        if text_key is None: text_key = 'main'
        if self.type == 'array':
            restexts = [v[text_key] for v in self.meta['lib']['values'][var]]
        else:
            values = emulate_meta(
                self.meta(), self.meta()['columns'][var].get('values', None))
            restexts = [v['text'][text_key] for v in values]
        return restexts

    def _switch_axes(self):
        """
        """
        if self.switched:
            self.switched = False
            self.matrix = self.matrix.swapaxes(1, 2)
        else:
            self.switched = True
            self.matrix = self.matrix.swapaxes(2, 1)
        self.xdef, self.ydef = self.ydef, self.xdef
        self._x_indexers, self._y_indexers = self._y_indexers, self._x_indexers
        self.comb_x, self.comb_y = self.comb_y, self.comb_x
        self.miss_x, self.miss_y = self.miss_y, self.miss_x
        return self

    def _reset(self):
        for prop in self.__dict__.keys():
            if prop in ['_uses_meta', 'base_all', '_dataidx', 'meta', '_cache',
                        'd', 'idx_map']:
                pass
            elif prop in ['_squeezed', 'switched']:
                self.__dict__[prop] = False
            else:
                self.__dict__[prop] = None
            self.result = None
        return None

    def swap(self, var, axis='x', inplace=True):
        """
        Change the Quantity's x- or y-axis keeping filter and weight setup.

        All edits and aggregation results will be removed during the swap.

        Parameters
        ----------
        var : str
            New variable's name used in axis swap.
        axis : {'x', 'y'}, default ``'x'``
            The axis to swap.
        inplace : bool, default ``True``
            Whether to modify the Quantity inplace or return a new instance.

        Returns
        -------
        swapped : New Quantity instance with exchanged x- or y-axis.
        """
        if axis == 'x':
            x = var
            y = self.y
        else:
            x = self.x
            y = var
        f, w = self.f, self.w
        if inplace:
            swapped = self
        else:
            swapped = self._copy()
        swapped._reset()
        swapped.x, swapped.y = x, y
        swapped.f, swapped.w = f, w
        swapped.type = swapped._get_type()
        swapped._get_matrix()
        if not inplace:
            return swapped

    def rescale(self, scaling, drop=False):
        """
        Modify the object's ``xdef`` property reflecting new value defintions.

        Parameters
        ----------
        scaling : dict
            Mapping of old_code: new_code, given as of type int or float.
        drop : bool, default False
            If True, codes not included in the scaling dict will be excluded.

        Returns
        -------
        self
        """
        proper_scaling = {old_code: new_code for old_code, new_code
                         in scaling.items() if old_code in self.xdef}
        xdef_ref = [proper_scaling[code] if code in proper_scaling.keys()
                    else code for code in self.xdef]
        if drop:
            to_drop = [code for code in self.xdef if code not in
                       proper_scaling.keys()]
            self.exclude(to_drop, axis='x')
        self.xdef = xdef_ref
        return self

    def exclude(self, codes, axis='x'):
        """
        Wrapper for _missingfy(...keep_codes=False, ..., keep_base=False, ...)
        Excludes specified codes from aggregation.
        """
        self._missingfy(codes, axis=axis, keep_base=False, inplace=True)
        return self

    def limit(self, codes, axis='x'):
        """
        Wrapper for _missingfy(...keep_codes=True, ..., keep_base=True, ...)
        Restrict the data matrix entires to contain the specified codes only.
        """
        self._missingfy(codes, axis=axis, keep_codes=True, keep_base=True,
                        inplace=True)
        return self

    def filter(self, condition, keep_base=True, inplace=False):
        """
        Use a Quantipy conditional expression to filter the data matrix entires.
        """
        if inplace:
            filtered = self
        else:
            filtered = self._copy()
        qualified_rows = self._get_logic_qualifiers(condition)
        valid_rows = self.idx_map[self.idx_map[:, 0] == 1][:, 1]
        filter_idx = np.in1d(valid_rows, qualified_rows)
        if keep_base:
            filtered.matrix[~filter_idx, 1:, :] = np.NaN
        else:
            filtered.matrix[~filter_idx, :, :] = np.NaN
        if not inplace:
            return filtered

    def _get_logic_qualifiers(self, condition):
        if not isinstance(condition, dict):
            column = self.x
            logic = condition
        else:
            column = condition.keys()[0]
            logic = condition.values()[0]
        idx, logical_expression = get_logic_index(self.d()[column], logic, self.d())
        logical_expression = logical_expression.split(':')[0]
        if not column == self.x:
            logical_expression = logical_expression.replace('x[', column+'[')
        self.logical_conditions.append(logical_expression)
        return idx

    def _missingfy(self, codes, axis='x', keep_codes=False, keep_base=True,
                   indices=False, inplace=True):
        """
        Clean matrix from entries preserving or modifying the weight vector.

        Parameters
        ----------
        codes : list
            A list of codes to be considered in cleaning.
        axis : {'x', 'y'}, default 'x'
            The axis to clean codes on. Refers to the Link object's x- and y-
            axes.
        keep_codes : bool, default False
            Controls whether the passed codes are kept or erased from the
            Quantity matrix data entries.
        keep_base: bool, default True
            Controls whether the weight vector is set to np.NaN alongside
            the x-section rows or remains unmodified.
        indices: bool, default False
            If ``True``, the data matrix indicies of the corresponding codes
            will be returned as well.
        inplace : bool, default True
            Will overwrite self.matrix with the missingfied matrix by default.
            If ``False``, the method will return a new np.array with the
            modified entries.

        Returns
        -------
        self or numpy.array (and optionally a list of int when ``indices=True``)
            Either a new matrix is returned as numpy.array or the ``matrix``
            property is modified inplace.
        """
        if inplace:
            missingfied = self
        else:
            missingfied = self._copy()
        if axis == 'y' and self.y == '@' and not self.type == 'array':
            return self
        elif axis == 'y' and self.type == 'array':
            ni_err = 'Cannot missingfy array mask element sections!'
            raise NotImplementedError(ni_err)
        else:
            if axis == 'y':
                missingfied._switch_axes()
            mis_ix = missingfied._get_drop_idx(codes, keep_codes)
            mis_ix = [code + 1 for code in mis_ix]
            if mis_ix is not None:
                for ix in mis_ix:
                    np.place(missingfied.matrix[:, ix],
                             missingfied.matrix[:, ix] > 0, np.NaN)
                if not keep_base:
                    if axis == 'x':
                        self.miss_x = codes
                    else:
                        self.miss_y = codes
                    if self.type == 'array':
                        mask = np.nansum(missingfied.matrix[:, missingfied._x_indexers],
                                         axis=1, keepdims=True)
                        mask /= mask
                        mask = mask > 0
                    else:
                        mask = np.nansum(np.sum(missingfied.matrix,
                                                axis=1, keepdims=False),
                                         axis=1, keepdims=True) > 0
                    missingfied.matrix[~mask] = np.NaN
                if axis == 'y':
                    missingfied._switch_axes()
            if inplace:
                self.matrix = missingfied.matrix
                if indices:
                    return mis_ix
            else:
                if indices:
                    return missingfied, mis_ix
                else:
                    return missingfied

    def _organize_missings(self, missings):
        hidden = [c for c in missings.keys() if missings[c] == 'hidden']
        excluded = [c for c in missings.keys() if missings[c] == 'excluded']
        shown = [c for c in missings.keys() if missings[c] == 'shown']
        return hidden, excluded, shown

    def _clean_from_missings(self):
        if self.ds()._has_missings(self.x):
            missings = self.ds()._get_missings(self.x)
            hidden, excluded, shown = self._organize_missings(missings)
            if excluded:
                excluded_codes = excluded
                excluded_idxer = self._missingfy(excluded, keep_base=False,
                                                 indices=True)
            else:
                excluded_codes, excluded_idxer = [], []
            if hidden:
                hidden_codes = hidden
                hidden_idxer = self._get_drop_idx(hidden, keep=False)
                hidden_idxer = [code + 1 for code in hidden_idxer]
            else:
                hidden_codes, hidden_idxer = [], []
            dropped_codes = excluded_codes + hidden_codes
            dropped_codes_idxer = excluded_idxer + hidden_idxer
            self._x_indexers = [x_idx for x_idx in self._x_indexers
                                if x_idx not in dropped_codes_idxer]
            self.matrix = self.matrix[:, [0] + self._x_indexers]
            self.xdef = [x_c for x_c in self.xdef if x_c not in dropped_codes]
        else:
            pass
        return None

    def _get_drop_idx(self, codes, keep):
        """
        Produces a list of indices referring to the given input matrix's axes
        sections in order to erase data entries.

        Parameters
        ----------
        codes : list
            Data codes that should be dropped from or kept in the matrix.
        keep : boolean
            Controls if the the passed code defintion is interpreted as
            "codes to keep" or "codes to drop".

        Returns
        -------
        drop_idx : list
            List of x section matrix indices.
        """
        if codes is None:
            return None
        else:
            if keep:
                return [self.xdef.index(code) for code in self.xdef
                        if code not in codes]
            else:
                return [self.xdef.index(code) for code in codes
                        if code in self.xdef]

    def group(self, groups, axis='x', expand=None, complete=False):
        """
        Build simple or logical net vectors, optionally keeping orginating codes.

        Parameters
        ----------
        groups : list, dict of lists or logic expression
            The group/net code defintion(s) in form of...

            * a simple list: ``[1, 2, 3]``
            * a dict of list: ``{'grp A': [1, 2, 3], 'grp B': [4, 5, 6]}``
            * a logical expression: ``not_any([1, 2])``

        axis : {``'x'``, ``'y'``}, default ``'x'``
            The axis to group codes on.
        expand : {None, ``'before'``, ``'after'``}, default ``None``
            If ``'before'``, the codes that are grouped will be kept and placed
            before the grouped aggregation; vice versa for ``'after'``. Ignored
            on logical expressions found in ``groups``.
        complete : bool, default False
            If True, codes that define the Link on the given ``axis`` but are
            not present in the ``groups`` defintion(s) will be placed in their
            natural position within the aggregation, respecting the value of
            ``expand``.

        Returns
        -------
        None
        """
        # check validity and clean combine instructions
        if axis == 'y' and self.type == 'array':
            ni_err_array = 'Array mask element sections cannot be combined.'
            raise NotImplementedError(ni_err_array)
        elif axis == 'y' and self.y == '@':
            val_err = 'Total link has no y-axis codes to combine.'
            raise ValueError(val_err)
        grp_def = self._organize_grp_def(groups, expand, complete, axis)
        combines = []
        names = []
        # generate the net vectors (+ possible expanded originating codes)
        for grp in grp_def:
            name, group, exp, logical = grp[0], grp[1], grp[2], grp[3]
            one_code = len(group) == 1
            if one_code and not logical:
                vec = self._slice_vec(group[0], axis=axis)
            elif not logical and not one_code:
                vec, idx = self._grp_vec(group, axis=axis)
            else:
                vec = self._logic_vec(group)
            if axis == 'y':
                self._switch_axes()
            if exp is not None:
                m_idx = [ix for ix in self._x_indexers if ix not in idx]
                m_idx = self._sort_indexer_as_codes(m_idx, group)
                if exp == 'after':
                    names.extend(name)
                    names.extend([c for c in group])
                    combines.append(
                        np.concatenate([vec, self.matrix[:, m_idx]], axis=1))
                else:
                    names.extend([c for c in group])
                    names.extend(name)
                    combines.append(
                        np.concatenate([self.matrix[:, m_idx], vec], axis=1))
            else:
                names.extend(name)
                combines.append(vec)
            if axis == 'y':
                self._switch_axes()
        # re-construct the combined data matrix
        combines = np.concatenate(combines, axis=1)
        if axis == 'y':
            self._switch_axes()
        combined_matrix = np.concatenate([self.matrix[:, [0]],
                                          combines], axis=1)
        if axis == 'y':
            combined_matrix = combined_matrix.swapaxes(1, 2)
            self._switch_axes()
        # update the sectional information
        new_sect_def = range(0, combined_matrix.shape[1] - 1)
        if axis == 'x':
            self.xdef = new_sect_def
            self._x_indexers = self._get_x_indexers()
            self.comb_x = names
        else:
            self.ydef = new_sect_def
            self._y_indexers = self._get_y_indexers()
            self.comb_y = names
        self.matrix = combined_matrix

    def _slice_vec(self, code, axis='x'):
        '''
        '''
        if axis == 'x':
            code_idx = self.xdef.index(code) + 1
        else:
            code_idx = self.ydef.index(code) + 1
        if axis == 'x':
            m_slice = self.matrix[:, [code_idx]]
        else:
            self._switch_axes()
            m_slice = self.matrix[:, [code_idx]]
            self._switch_axes()
        return m_slice

    def _grp_vec(self, codes, axis='x'):
        netted, idx = self._missingfy(codes=codes, axis=axis,
                                      keep_codes=True, keep_base=True,
                                      indices=True, inplace=False)
        if axis == 'y':
            netted._switch_axes()
        net_vec = np.nansum(netted.matrix[:, netted._x_indexers],
                            axis=1, keepdims=True)
        net_vec /= net_vec
        return net_vec, idx

    def _logic_vec(self, condition):
        """
        Create net vector of qualified rows based on passed condition.
        """
        filtered = self.filter(condition=condition, inplace=False)
        net_vec = np.nansum(filtered.matrix[:, self._x_indexers], axis=1,
                            keepdims=True)
        net_vec /= net_vec
        return net_vec

    def _grp_type(self, grp_def):
        if isinstance(grp_def, list):
            if not isinstance(grp_def[0], (int, float)):
                return 'block'
            else:
                return 'list'
        elif isinstance(grp_def, tuple):
            return 'logical'
        elif isinstance(grp_def, dict):
            return 'wildcard'

    def _add_unused_codes(self, grp_def_list, axis):
        '''
        '''
        query_codes = self.xdef if axis == 'x' else self.ydef
        frame_lookup = {c: [[c], [c], None, False] for c in query_codes}
        frame = [[code] for code in query_codes]
        for grpdef_idx, grpdef in enumerate(grp_def_list):
            for code in grpdef[1]:
                if [code] in frame:
                    if grpdef not in frame:
                        frame[frame.index([code])] = grpdef
                    else:
                        frame[frame.index([code])] = '-'
        frame = [code for code in frame if not code == '-']
        for code in frame:
            if code[0] in frame_lookup.keys():
               frame[frame.index([code[0]])] = frame_lookup[code[0]]
        return frame

    def _organize_grp_def(self, grp_def, method_expand, complete, axis):
        """
        Sanitize a combine instruction list (of dicts): names, codes, expands.
        """
        organized_def = []
        codes_used = []
        any_extensions = complete
        any_logical = False
        if method_expand is None and complete:
            method_expand = 'before'
        if not self._grp_type(grp_def) == 'block':
            grp_def = [{'net': grp_def, 'expand': method_expand}]
        for grp in grp_def:
            if self._grp_type(grp.values()[0]) in ['logical', 'wildcard']:
                if complete:
                    ni_err = ('Logical expr. unsupported when complete=True. '
                              'Only list-type nets/groups can be completed.')
                    raise NotImplementedError(ni_err)
                if 'expand' in grp.keys():
                    del grp['expand']
                expand = None
                logical = True
            else:
                if 'expand' in grp.keys():
                    grp = copy.deepcopy(grp)
                    expand = grp['expand']
                    if expand is None and complete:
                        expand = 'before'
                    del grp['expand']
                else:
                    expand = method_expand
                logical = False
            organized_def.append([grp.keys(), grp.values()[0], expand, logical])
            if expand:
                any_extensions = True
            if logical:
                any_logical = True
            codes_used.extend(grp.values()[0])
        if not any_logical:
            if len(set(codes_used)) != len(codes_used) and any_extensions:
                ni_err_extensions = ('Same codes in multiple groups unsupported '
                                     'with expand and/or complete =True.')
                raise NotImplementedError(ni_err_extensions)
        if complete:
            return self._add_unused_codes(organized_def, axis)
        else:
            return organized_def

    def _force_to_nparray(self):
        """
        Convert the aggregation result into its numpy array equivalent.
        """
        if isinstance(self.result, pd.DataFrame):
            self.result = self.result.values
            return True
        else:
            return False

    def _attach_margins(self):
        """
        Force margins back into the current Quantity.result if none are found.
        """
        if not self._res_is_stat():
            values = self.result
            if not self._has_y_margin and not self.y == '@':
                margins = False
                values = np.concatenate([self.rbase[1:, :], values], 1)
            else:
                margins = True
            if not self._has_x_margin:
                margins = False
                values = np.concatenate([self.cbase, values], 0)
            else:
                margins = True
            self.result = values
            return margins
        else:
            return False

    def _organize_expr_def(self, expression, axis):
        """
        """
        # Prepare expression parts and lookups for indexing the agg. result
        val1, op, val2 = expression[0], expression[1], expression[2]
        if self._res_is_stat():
            idx_c = [self.current_agg]
            offset = 0
        else:
            if axis == 'x':
                idx_c = self.xdef if not self.comb_x else self.comb_x
            else:
                idx_c = self.ydef if not self.comb_y else self.comb_y
            offset = 1
        # Test expression validity and find np.array indices / prepare scalar
        # values of the expression
        idx_err = '"{}" not found in {}-axis.'
        # [1] input is 1. scalar, 2. vector from the agg. result
        if isinstance(val1, list):
            if not val2 in idx_c:
                raise IndexError(idx_err.format(val2, axis))
            val1 = val1[0]
            val2 = idx_c.index(val2) + offset
            expr_type = 'scalar_1'
        # [2] input is 1. vector from the agg. result, 2. scalar
        elif isinstance(val2, list):
            if not val1 in idx_c:
                raise IndexError(idx_err.format(val1, axis))
            val1 = idx_c.index(val1) + offset
            val2 = val2[0]
            expr_type = 'scalar_2'
        # [3] input is two vectors from the agg. result
        elif not any(isinstance(val, list) for val in [val1, val2]):
            if not val1 in idx_c:
                raise IndexError(idx_err.format(val1, axis))
            if not val2 in idx_c:
                raise IndexError(idx_err.format(val2, axis))
            val1 = idx_c.index(val1) + offset
            val2 = idx_c.index(val2) + offset
            expr_type = 'vectors'
        return val1, op, val2, expr_type, idx_c

    @staticmethod
    def constant(num):
        return [num]

    def calc(self, expression, axis='x', result_only=False):
        """
        Compute (simple) aggregation level arithmetics.
        """
        unsupported = ['cbase', 'rbase', 'summary', 'x_sum', 'y_sum']
        if self.result is None:
            raise ValueError('No aggregation to base calculation on.')
        elif self.current_agg in unsupported:
            ni_err = 'Aggregation type "{}" not supported.'
            raise NotImplementedError(ni_err.format(self.current_agg))
        elif axis not in ['x', 'y']:
            raise ValueError('Invalid axis parameter: {}'.format(axis))
        is_df = self._force_to_nparray()
        has_margin = self._attach_margins()
        values = self.result
        expr_name = expression.keys()[0]
        if axis == 'x':
            self.calc_x = expr_name
        else:
            self.calc_y = expr_name
            values = values.T
        expr = expression.values()[0]
        v1, op, v2, exp_type, index_codes = self._organize_expr_def(expr, axis)
        # ====================================================================
        # TODO: generalize this calculation part so that it can "parse"
        # arbitrary calculation rules given as nested or concatenated
        # operators/codes sequences.
        if exp_type == 'scalar_1':
            val1, val2 = v1, values[[v2], :]
        elif exp_type == 'scalar_2':
            val1, val2 = values[[v1], :], v2
        elif exp_type == 'vectors':
            val1, val2 = values[[v1], :], values[[v2], :]
        calc_res = op(val1, val2)
        # ====================================================================
        if axis == 'y':
            calc_res = calc_res.T
        ap_axis = 0 if axis == 'x' else 1
        if result_only:
            if not self._res_is_stat():
                self.result = np.concatenate([self.result[[0], :], calc_res],
                                             ap_axis)
            else:
                self.result = calc_res
        else:
            self.result = np.concatenate([self.result, calc_res], ap_axis)
            if axis == 'x':
                self.calc_x = index_codes + [self.calc_x]
            else:
                self.calc_y = index_codes + [self.calc_y]
        self.cbase = self.result[[0], :]
        if self.type in ['simple', 'nested']:
            self.rbase = self.result[:, [0]]
        else:
            self.rbase = None
        if not self._res_is_stat():
            self.current_agg = 'calc'
            self._organize_margins(has_margin)
        else:
            self.current_agg = 'calc'
        if is_df:
            self.to_df()
        return self

    def count(self, axis=None, raw_sum=False, margin=True, as_df=True):
        """
        Count entries over all cells or per axis margin.

        Parameters
        ----------
        axis : {None, 'x', 'y'}, deafult None
            When axis is None, the frequency of all cells from the uni- or
            multivariate distribution is presented. If the axis is specified
            to be either 'x' or 'y' the margin per axis becomes the resulting
            aggregation.
        raw_sum : bool, default False
            If True will perform a simple summation over the cells given the
            axis parameter. This ignores net counting of qualifying answers in
            favour of summing over all answers given when considering margins.
        margin : bool, deafult True
            Controls whether the margins of the aggregation result are shown.
            This also applies to margin aggregations themselves, since they
            contain a margin in (form of the total number of cases) as well.
        as_df : bool, default True
            Controls whether the aggregation is transformed into a Quantipy-
            multiindexed (following the Question/Values convention)
            pandas.DataFrame or will be left in its numpy.array format.

        Returns
        -------
        self
            Passes a pandas.DataFrame or numpy.array of cell or margin counts
            to the ``result`` property.
        """
        if axis is None and raw_sum:
            raise ValueError('Cannot calculate raw sum without axis.')
        if axis is None:
            self.current_agg = 'freq'
        elif axis == 'x':
            self.current_agg = 'cbase' if not raw_sum else 'x_sum'
        elif axis == 'y':
            self.current_agg = 'rbase' if not raw_sum else 'y_sum'
        if not self.w == '@1':
            self.weight()
        if not self.is_empty or self._uses_meta:
            counts = np.nansum(self.matrix, axis=0)
        else:
            counts = self._empty_result()
        self.cbase = counts[[0], :]
        if self.type in ['simple', 'nested']:
            self.rbase = counts[:, [0]]
        else:
            self.rbase = None
        if axis is None:
            self.result = counts
        elif axis == 'x':
            if not raw_sum:
                self.result = counts[[0], :]
            else:
                self.result = np.nansum(counts[1:, :], axis=0, keepdims=True)
        elif axis == 'y':
            if not raw_sum:
                self.result = counts[:, [0]]
            else:
                if self.x == '@' or self.y == '@':
                    self.result = counts[:, [0]]
                else:
                    self.result = np.nansum(counts[:, 1:], axis=1, keepdims=True)
        self._organize_margins(margin)
        if as_df:
            self.to_df()
        self.unweight()
        return self

    def _empty_result(self):
        if self._res_is_stat() or self.current_agg == 'summary':
            self.factorized = 'x'
            xdim = 1 if self._res_is_stat() else 8
            if self.ydef is None:
                ydim = 1
            elif self.ydef is not None and len(self.ydef) == 0:
                ydim = 2
            else:
                ydim = len(self.ydef) + 1
        else:
            if self.xdef is not None:
                if len(self.xdef) == 0:
                    xdim = 2
                else:
                    xdim = len(self.xdef) + 1
                if self.ydef is None:
                    ydim = 1
                elif self.ydef is not None and len(self.ydef) == 0:
                    ydim = 2
                else:
                    ydim = len(self.ydef) + 1
            elif self.xdef is None:
                xdim = 2
                if self.ydef is None:
                    ydim = 1
                elif self.ydef is not None and len(self.ydef) == 0:
                    ydim = 2
                else:
                    ydim = len(self.ydef) + 1
        return np.zeros((xdim, ydim))

    def _effective_n(self, axis=None, margin=True):
        self.weight()
        effective = (np.nansum(self.matrix, axis=0)**2 /
                     np.nansum(self.matrix**2, axis=0))
        self.unweight()
        start_on = 0 if margin else 1
        if axis is None:
            return effective[start_on:, start_on:]
        elif axis == 'x':
            return effective[[0], start_on:]
        else:
            return effective[start_on:, [0]]

    def summarize(self, stat='summary', axis='x', margin=True, as_df=True):
        """
        Calculate distribution statistics across the given axis.

        Parameters
        ----------
        stat : {'summary', 'mean', 'median', 'var', 'stddev', 'sem', varcoeff',
                'min', 'lower_q', 'upper_q', 'max'}, default 'summary'
            The measure to calculate. Defaults to a summary output of the most
            important sample statistics.
        axis : {'x', 'y'}, default 'x'
            The axis which is reduced in the aggregation, e.g. column vs. row
            means.
        margin : bool, default True
            Controls whether statistic(s) of the marginal distribution are
            shown.
        as_df : bool, default True
            Controls whether the aggregation is transformed into a Quantipy-
            multiindexed (following the Question/Values convention)
            pandas.DataFrame or will be left in its numpy.array format.

        Returns
        -------
        self
            Passes a pandas.DataFrame or numpy.array of the descriptive (summary)
            statistic(s) to the ``result`` property.
        """
        self.current_agg = stat
        if self.is_empty:
            self.result = self._empty_result()
        else:
            if stat == 'summary':
                stddev, mean, base = self._dispersion(axis, measure='sd',
                                                      _return_mean=True,
                                                      _return_base=True)
                self.result = np.concatenate([
                    base, mean, stddev,
                    self._min(axis),
                    self._percentile(perc=0.25),
                    self._percentile(perc=0.50),
                    self._percentile(perc=0.75),
                    self._max(axis)
                    ], axis=0)
            elif stat == 'mean':
                self.result = self._means(axis)
            elif stat == 'var':
                self.result = self._dispersion(axis, measure='var')
            elif stat == 'stddev':
                self.result = self._dispersion(axis, measure='sd')
            elif stat == 'sem':
                self.result = self._dispersion(axis, measure='sem')
            elif stat == 'varcoeff':
                self.result = self._dispersion(axis, measure='varcoeff')
            elif stat == 'min':
                self.result = self._min(axis)
            elif stat == 'lower_q':
                self.result = self._percentile(perc=0.25)
            elif stat == 'median':
                self.result = self._percentile(perc=0.5)
            elif stat == 'upper_q':
                self.result = self._percentile(perc=0.75)
            elif stat == 'max':
                self.result = self._max(axis)
        self._organize_margins(margin)
        if as_df:
            self.to_df()
        return self

    def _factorize(self, axis='x', inplace=True):
        self.factorized = axis
        if inplace:
            factorized = self
        else:
            factorized = self._copy()
        if axis == 'y':
            factorized._switch_axes()
        np.copyto(factorized.matrix[:, 1:, :],
                  np.atleast_3d(factorized.xdef),
                  where=factorized.matrix[:, 1:, :]>0)
        if not inplace:
            return factorized

    def _means(self, axis, _return_base=False):
        fact = self._factorize(axis=axis, inplace=False)
        if not self.w == '@1':
            fact.weight()
        fact_prod = np.nansum(fact.matrix, axis=0)
        fact_prod_sum = np.nansum(fact_prod[1:, :], axis=0, keepdims=True)
        bases = fact_prod[[0], :]
        means = fact_prod_sum/bases
        if axis == 'y':
            self._switch_axes()
            means = means.T
            bases = bases.T
        if _return_base:
            return means, bases
        else:
            return means

    def _dispersion(self, axis='x', measure='sd', _return_mean=False,
                    _return_base=False):
        """
        Extracts measures of dispersion from the incoming distribution of
        X vs. Y. Can return the arithm. mean by request as well. Dispersion
        measure supported are standard deviation, variance, coeffiecient of
        variation and standard error of the mean.
        """
        means, bases = self._means(axis, _return_base=True)
        unbiased_n = bases - 1
        self.unweight()
        factorized = self._factorize(axis, inplace=False)
        factorized.matrix[:, 1:] -= means
        factorized.matrix[:, 1:] *= factorized.matrix[:, 1:, :]
        if not self.w == '@1':
            factorized.weight()
        diff_sqrt = np.nansum(factorized.matrix[:, 1:], axis=1)
        disp = np.nansum(diff_sqrt/unbiased_n, axis=0, keepdims=True)
        disp[disp <= 0] = np.NaN
        disp[np.isinf(disp)] = np.NaN
        if measure == 'sd':
            disp = np.sqrt(disp)
        elif measure == 'sem':
            disp = np.sqrt(disp) / np.sqrt((unbiased_n + 1))
        elif measure == 'varcoeff':
            disp = np.sqrt(disp) / means
        self.unweight()
        if _return_mean and _return_base:
            return disp, means, bases
        elif _return_mean:
            return disp, means
        elif _return_base:
            return disp, bases
        else:
            return disp

    def _max(self, axis='x'):
        factorized = self._factorize(axis, inplace=False)
        vals = np.nansum(factorized.matrix[:, 1:, :], axis=1)
        return np.nanmax(vals, axis=0, keepdims=True)

    def _min(self, axis='x'):
        factorized = self._factorize(axis, inplace=False)
        vals = np.nansum(factorized.matrix[:, 1:, :], axis=1)
        if 0 not in factorized.xdef: np.place(vals, vals == 0, np.inf)
        return np.nanmin(vals, axis=0, keepdims=True)

    def _percentile(self, axis='x', perc=0.5):
        """
        Computes percentiles from the incoming distribution of X vs.Y and the
        requested percentile value. The implementation mirrors the algorithm
        used in SPSS Dimensions and the EXAMINE procedure in SPSS Statistics.
        It based on the percentile defintion #6 (adjusted for survey weights)
        in:
        Hyndman, Rob J. and Fan, Yanan (1996) -
        "Sample Quantiles in Statistical Packages",
        The American Statistician, 50, No. 4, 361-365.

        Parameters
        ----------
        axis : {'x', 'y'}, default 'x'
            The axis which is reduced in the aggregation, i.e. column vs. row
            medians.
        perc : float, default 0.5
            Defines the percentile to be computed. Defaults to 0.5,
            the sample median.

        Returns
        -------
        percs : np.array
            Numpy array storing percentile values.
        """
        percs = []
        factorized = self._factorize(axis, inplace=False)
        vals = np.nansum(np.nansum(factorized.matrix[:, 1:, :], axis=1,
                                   keepdims=True), axis=1)
        weights = (vals/vals)*self.wv
        for shape_i in range(0, vals.shape[1]):
            iter_weights = weights[:, shape_i]
            iter_vals = vals[:, shape_i]
            mask = ~np.isnan(iter_weights)
            iter_weights = iter_weights[mask]
            iter_vals = iter_vals[mask]
            sorter = np.argsort(iter_vals)
            iter_vals = np.take(iter_vals, sorter)
            iter_weights = np.take(iter_weights, sorter)
            iter_wsum = np.nansum(iter_weights, axis=0)
            iter_wcsum = np.cumsum(iter_weights, axis=0)
            k = (iter_wsum + 1.0) * perc
            if iter_vals.shape[0] == 0:
                percs.append(0.00)
            elif iter_vals.shape[0] == 1:
                percs.append(iter_vals[0])
            elif iter_wcsum[0] > k:
                wcsum_k = iter_wcsum[0]
                percs.append(iter_vals[0])
            elif iter_wcsum[-1] <= k:
                percs.append(iter_vals[-1])
            else:
                wcsum_k = iter_wcsum[iter_wcsum <= k][-1]
                p_k_idx = np.searchsorted(np.ndarray.flatten(iter_wcsum), wcsum_k)
                p_k = iter_vals[p_k_idx]
                p_k1 = iter_vals[p_k_idx+1]
                w_k1 = iter_weights[p_k_idx+1]
                excess = k - wcsum_k
                if excess >= 1.0:
                    percs.append(p_k1)
                else:
                    if w_k1 >= 1.0:
                        percs.append((1.0-excess)*p_k + excess*p_k1)
                    else:
                        percs.append((1.0-(excess/w_k1))*p_k +
                                     (excess/w_k1)*p_k1)
        return np.array(percs)[None, :]

    def _organize_margins(self, margin):
        if self._res_is_stat():
            if self.type == 'array' or self.y == '@' or self.x == '@':
                self._has_y_margin = self._has_x_margin = False
            else:
                if self.factorized == 'x':
                    if not margin:
                        self._has_x_margin = False
                        self._has_y_margin = False
                        self.result = self.result[:, 1:]
                    else:
                        self._has_x_margin = False
                        self._has_y_margin = True
                else:
                    if not margin:
                        self._has_x_margin = False
                        self._has_y_margin = False
                        self.result = self.result[1:, :]
                    else:
                        self._has_x_margin = True
                        self._has_y_margin = False
        if self._res_is_margin():
            if self.y == '@' or self.x == '@':
                if self.current_agg in ['cbase', 'x_sum']:
                    self._has_y_margin = self._has_x_margin = False
                if self.current_agg in ['rbase', 'y_sum']:
                    if not margin:
                        self._has_y_margin = self._has_x_margin = False
                        self.result = self.result[1:, :]
                    else:
                        self._has_x_margin = True
                        self._has_y_margin = False
            else:
                if self.current_agg in ['cbase', 'x_sum']:
                    if not margin:
                        self._has_y_margin = self._has_x_margin = False
                        self.result = self.result[:, 1:]
                    else:
                        self._has_x_margin = False
                        self._has_y_margin = True
                if self.current_agg in ['rbase', 'y_sum']:
                    if not margin:
                        self._has_y_margin = self._has_x_margin = False
                        self.result = self.result[1:, :]
                    else:
                        self._has_x_margin = True
                        self._has_y_margin = False
        elif self.current_agg in ['freq', 'summary', 'calc']:
            if self.type == 'array' or self.y == '@' or self.x == '@':
                if not margin:
                    self.result = self.result[1:, :]
                    self._has_x_margin = False
                    self._has_y_margin = False
                else:
                    self._has_x_margin = True
                    self._has_y_margin = False
            else:
                if not margin:
                    self.result = self.result[1:, 1:]
                    self._has_x_margin = False
                    self._has_y_margin = False
                else:
                    self._has_x_margin = True
                    self._has_y_margin = True
        else:
            pass

    def _sort_indexer_as_codes(self, indexer, codes):
        mapping = sorted(zip(indexer, codes), key=lambda l: l[1])
        return [i[0] for i in mapping]

    def _get_y_indexers(self):
        if self._squeezed or self.type in ['simple', 'nested']:
            if self.ydef is not None:
                idxs = range(1, len(self.ydef)+1)
                return self._sort_indexer_as_codes(idxs, self.ydef)
            else:
                return [1]
        else:
            y_indexers = []
            xdef_len = len(self.xdef)
            zero_based_ys = [idx for idx in xrange(0, xdef_len)]
            for y_no in xrange(0, len(self.ydef)):
                if y_no == 0:
                    y_indexers.append(zero_based_ys)
                else:
                    y_indexers.append([idx + y_no * xdef_len
                                       for idx in zero_based_ys])
        return y_indexers

    def _get_x_indexers(self):
        if self._squeezed or self.type in ['simple', 'nested']:
            idxs = range(1, len(self.xdef)+1)
            return self._sort_indexer_as_codes(idxs, self.xdef)
        else:
            x_indexers = []
            upper_x_idx = len(self.ydef)
            start_x_idx = [len(self.xdef) * offset
                           for offset in range(0, upper_x_idx)]
            for x_no in range(0, len(self.xdef)):
                x_indexers.append([idx + x_no for idx in start_x_idx])
            return x_indexers

    def _squeeze_dummies(self):
        """
        Reshape and replace initial 2D dummy matrix into its 3D equivalent.
        """
        self.wv = self.matrix[:, [-1]]
        sects = []
        if self.type == 'array':
            x_sections = self._get_x_indexers()
            y_sections = self._get_y_indexers()
            y_total = np.nansum(self.matrix[:, x_sections], axis=1)
            y_total /= y_total
            y_total = y_total[:, None, :]
            for sect in y_sections:
                sect = self.matrix[:, sect]
                sects.append(sect)
            sects = np.dstack(sects)
            self._squeezed = True
            sects = np.concatenate([y_total, sects], axis=1)
            self.matrix = sects
            self._x_indexers = self._get_x_indexers()
            self._y_indexers = []
        elif self.type in ['simple', 'nested']:
            x = self.matrix[:, :len(self.xdef)+1]
            y = self.matrix[:, len(self.xdef)+1:-1]
            for i in range(0, y.shape[1]):
                sects.append(x * y[:, [i]])
            sects = np.dstack(sects)
            self._squeezed = True
            self.matrix = sects
            self._x_indexers = self._get_x_indexers()
            self._y_indexers = self._get_y_indexers()
        #=====================================================================
        #THIS CAN SPEED UP PERFOMANCE BY A GOOD AMOUNT BUT STACK-SAVING
        #TIME & SIZE WILL SUFFER. WE CAN DEL THE "SQUEEZED" COLLECTION AT
        #SAVE STAGE.
        #=====================================================================
        # self.cache().set_obj(collection='squeezed',
        #                     key=self.f+self.w+self.x+self.y,
        #                     obj=(self.xdef, self.ydef,
        #                          self._x_indexers, self._y_indexers,
        #                          self.wv, self.matrix, self.idx_map))

    def _get_matrix(self):
        wv = self.cache().get_obj('weight_vectors', self.w)
        # wv = None
        if wv is None:
            wv = self._get_wv()
            self.cache().set_obj('weight_vectors', self.w, wv)
        total = self.cache().get_obj('weight_vectors', '@1')
        # total = None
        if total is None:
            total = self._get_total()
            self.cache().set_obj('weight_vectors', '@1', total)
        if self.type == 'array':
            xm, self.xdef, self.ydef = self._dummyfy()
            self.matrix = np.concatenate((xm, wv), 1)
        else:
            if self.y == '@' or self.x == '@':
                section = self.x if self.y == '@' else self.y
                xm, self.xdef = self.cache().get_obj('matrices', section)
                #xm = None
                if xm is None:
                    xm, self.xdef = self._dummyfy(section)
                    self.cache().set_obj('matrices', section, (xm, self.xdef))
                self.ydef = None
                self.matrix = np.concatenate((total, xm, total, wv), 1)
            else:
                xm, self.xdef = self.cache().get_obj('matrices', self.x)
                # xm = None
                if xm is None:
                    xm, self.xdef = self._dummyfy(self.x)
                    self.cache().set_obj('matrices', self.x, (xm, self.xdef))
                ym, self.ydef = self.cache().get_obj('matrices', self.y)
                if ym is None:
                    ym, self.ydef = self._dummyfy(self.y)
                    self.cache().set_obj('matrices', self.y, (ym, self.ydef))
                self.matrix = np.concatenate((total, xm, total, ym, wv), 1)
        self.matrix = self.matrix[self._dataidx]
        self.matrix = self._clean()
        self._squeeze_dummies()
        self._clean_from_missings()
        return self.matrix

    def _dummyfy(self, section=None):
        if section is not None:
            # i.e. Quantipy multicode data
            if self.d()[section].dtype == 'object':
                section_data = self.d()[section].str.get_dummies(';')
                if self._uses_meta:
                    res_codes = self._get_response_codes(section)
                    section_data.columns = [int(col) for col in section_data.columns]
                    section_data = section_data.reindex(columns=res_codes)
                    section_data.replace(np.NaN, 0, inplace=True)
                if not self._uses_meta:
                    section_data.sort_index(axis=1, inplace=True)
            # i.e. Quantipy single-coded/numerical data
            else:
                section_data = pd.get_dummies(self.d()[section])
                if self._uses_meta and not self._is_raw_numeric(section):
                    res_codes = self._get_response_codes(section)
                    section_data = section_data.reindex(columns=res_codes)
                    section_data.replace(np.NaN, 0, inplace=True)
                section_data.rename(
                    columns={
                        col: int(col)
                        if float(col).is_integer()
                        else col
                        for col in section_data.columns
                    },
                    inplace=True)
            return section_data.values, section_data.columns.tolist()
        elif section is None and self.type == 'array':
            a_i = [i['source'].split('@')[-1] for i in
                   self.meta()['masks'][self.x]['items']]
            a_res = self._get_response_codes(self.x)
            dummies = []
            if self._is_multicode_array(a_i[0]):
                for i in a_i:
                    i_dummy = self.d()[i].str.get_dummies(';')
                    i_dummy.columns = [int(col) for col in i_dummy.columns]
                    dummies.append(i_dummy.reindex(columns=a_res))
            else:
                for i in a_i:
                    dummies.append(pd.get_dummies(self.d()[i]).reindex(columns=a_res))
            a_data = pd.concat(dummies, axis=1)
            return a_data.values, a_res, a_i

    def _clean(self):
        """
        Drop empty sectional rows from the matrix.
        """
        mat = self.matrix.copy()
        mat_indexer = np.expand_dims(self._dataidx, 1)
        if not self.type == 'array':
            xmask = (np.nansum(mat[:, 1:len(self.xdef)+1], axis=1) > 0)
            if self.ydef is not None:
                if self.base_all:
                    ymask = (np.nansum(mat[:, len(self.xdef)+1:-1], axis=1) > 0)
                else:
                    ymask = (np.nansum(mat[:, len(self.xdef)+2:-1], axis=1) > 0)
                self.idx_map = np.concatenate(
                    [np.expand_dims(xmask & ymask, 1), mat_indexer], axis=1)
                return mat[xmask & ymask]
            else:
                self.idx_map = np.concatenate(
                    [np.expand_dims(xmask, 1), mat_indexer], axis=1)
                return mat[xmask]
        else:
            mask = (np.nansum(mat[:, :-1], axis=1) > 0)
            self.idx_map = np.concatenate(
                [np.expand_dims(mask, 1), mat_indexer], axis=1)
            return mat[mask]

    def _is_raw_numeric(self, var):
        return self.meta()['columns'][var]['type'] in ['int', 'float']

    def _res_from_count(self):
        return self._res_is_margin() or self.current_agg == 'freq'

    def _res_from_summarize(self):
        return self._res_is_stat() or self.current_agg == 'summary'

    def _res_is_margin(self):
        return self.current_agg in ['tbase', 'cbase', 'rbase', 'x_sum', 'y_sum']

    def _res_is_stat(self):
        return self.current_agg in ['mean', 'min', 'max', 'varcoeff', 'sem',
                                    'stddev', 'var', 'median', 'upper_q',
                                    'lower_q']
    def to_df(self):
        if self.current_agg == 'freq':
            if not self.comb_x:
                self.x_agg_vals = self.xdef
            else:
                self.x_agg_vals = self.comb_x
            if not self.comb_y:
                self.y_agg_vals = self.ydef
            else:
                self.y_agg_vals = self.comb_y
        elif self.current_agg == 'calc':
            if self.calc_x:
                self.x_agg_vals = self.calc_x
                self.y_agg_vals = self.ydef if not self.comb_y else self.comb_y
            else:
                self.x_agg_vals = self.xdef if not self.comb_x else self.comb_x
                self.y_agg_vals = self.calc_y
        elif self.current_agg == 'summary':
            summary_vals = ['mean', 'stddev', 'min', '25%',
                            'median', '75%', 'max']
            self.x_agg_vals = summary_vals
            self.y_agg_vals = self.ydef
        elif self.current_agg in ['x_sum', 'cbase']:
            self.x_agg_vals = 'All' if self.current_agg == 'cbase' else 'sum'
            self.y_agg_vals = self.ydef
        elif self.current_agg in ['y_sum', 'rbase']:
            self.x_agg_vals = self.xdef
            self.y_agg_vals = 'All' if self.current_agg == 'rbase' else 'sum'
        elif self._res_is_stat():
            if self.factorized == 'x':
                self.x_agg_vals = self.current_agg
                self.y_agg_vals = self.ydef if not self.comb_y else self.comb_y
            else:
                self.x_agg_vals = self.xdef if not self.comb_x else self.comb_x
                self.y_agg_vals = self.current_agg
        # can this made smarter WITHOUT 1000000 IF-ELSEs above?:
        if ((self.current_agg in ['freq', 'cbase', 'x_sum', 'summary', 'calc'] or
                self._res_is_stat()) and not self.type == 'array'):
            if self.y == '@' or self.x == '@':
                self.y_agg_vals = '@'
        df = pd.DataFrame(self.result)
        idx, cols = self._make_multiindex()
        df.index = idx
        df.columns = cols
        self.result = df if not self.x == '@' else df.T
        if self.type == 'nested':
            self._format_nested_axis()
        return self

    def _make_multiindex(self):
        x_grps = self.x_agg_vals
        y_grps = self.y_agg_vals
        if not isinstance(x_grps, list):
            x_grps = [x_grps]
        if not isinstance(y_grps, list):
            y_grps = [y_grps]
        if not x_grps: x_grps = [None]
        if not y_grps: y_grps = [None]
        if self._has_x_margin:
            x_grps = ['All'] + x_grps
        if self._has_y_margin:
            y_grps = ['All'] + y_grps
        if self.type == 'array':
            x_unit = y_unit = self.x
            x_names = ['Question', 'Values']
            y_names = ['Array', 'Questions']
        else:
            x_unit = self.x if not self.x == '@' else self.y
            y_unit = self.y if not self.y == '@' else self.x
            x_names = y_names = ['Question', 'Values']
        x = [x_unit, x_grps]
        y = [y_unit, y_grps]
        index = pd.MultiIndex.from_product(x, names=x_names)
        columns = pd.MultiIndex.from_product(y, names=y_names)
        return index, columns

    def _format_nested_axis(self):
        nest_mi = self._make_nest_multiindex()
        if not len(self.result.columns) > len(nest_mi.values):
            self.result.columns = nest_mi
        else:
            total_mi_values = []
            for var in self.nest_def['variables']:
                total_mi_values += [var, -1]
            total_mi = pd.MultiIndex.from_product(total_mi_values,
                                                  names=nest_mi.names)
            full_nest_mi = nest_mi.union(total_mi)
            for lvl, c in zip(range(1, len(full_nest_mi)+1, 2),
                              self.nest_def['level_codes']):
                full_nest_mi.set_levels(['All'] + c, level=lvl, inplace=True)
            self.result.columns = full_nest_mi
        return None

    def _make_nest_multiindex(self):
        values = []
        names = ['Question', 'Values'] * (self.nest_def['levels'])
        for lvl_var, lvl_c in zip(self.nest_def['variables'],
                                  self.nest_def['level_codes']):
            values.append(lvl_var)
            values.append(lvl_c)
        mi = pd.MultiIndex.from_product(values, names=names)
        return mi

    def normalize(self, on='y'):
        """
        Convert a raw cell count result to its percentage representation.

        Parameters
        ----------
        on : {'y', 'x'}, default 'y'
            Defines the base to normalize the result on. ``'y'`` will
            produce column percentages, ``'x'`` will produce row
            percentages.

        Returns
        -------
        self
            Updates an count-based aggregation in the ``result`` property.
        """
        if self.x == '@':
            on = 'y' if on == 'x' else 'x'
        if on == 'y':
            if self._has_y_margin or self.y == '@' or self.x == '@':
                base = self.cbase
            else:
                base = self.cbase[:, 1:]
        else:
            if self._has_x_margin:
                base = self.rbase
            else:
                base = self.rbase[1:, :]
        if isinstance(self.result, pd.DataFrame):
            if self.x == '@':
                self.result = self.result.T
            if on == 'y':
                base = np.repeat(base, self.result.shape[0], axis=0)
            else:
                base = np.repeat(base, self.result.shape[1], axis=1)
        self.result = self.result / base * 100
        if self.x == '@':
            self.result = self.result.T
        return self

    def rebase(self, reference, on='counts', overwrite_margins=True):
        """
        """
        val_err = 'No frequency aggregation to rebase.'
        if self.result is None:
            raise ValueError(val_err)
        elif self.current_agg != 'freq':
            raise ValueError(val_err)
        is_df = self._force_to_nparray()
        has_margin = self._attach_margins()
        ref = self.swap(var=reference, inplace=False)
        if self._sects_identical(self.xdef, ref.xdef):
            pass
        elif self._sects_different_order(self.xdef, ref.xdef):
            ref.xdef = self.xdef
            ref._x_indexers = ref._get_x_indexers()
            ref.matrix = ref.matrix[:, ref._x_indexers + [0]]
        elif self._sect_is_subset(self.xdef, ref.xdef):
            ref.xdef = [code for code in ref.xdef if code in self.xdef]
            ref._x_indexers = ref._sort_indexer_as_codes(ref._x_indexers,
                                                         self.xdef)
            ref.matrix = ref.matrix[:, [0] + ref._x_indexers]
        else:
            idx_err = 'Axis defintion is not a subset of rebase reference.'
            raise IndexError(idx_err)
        ref_freq = ref.count(as_df=False)
        self.result = (self.result/ref_freq.result) * 100
        if overwrite_margins:
            self.rbase = ref_freq.rbase
            self.cbase = ref_freq.cbase
        self._organize_margins(has_margin)
        if is_df: self.to_df()
        return self

    @staticmethod
    def _sects_identical(axdef1, axdef2):
        return axdef1 == axdef2

    @staticmethod
    def _sects_different_order(axdef1, axdef2):
        if not len(axdef1) == len(axdef2):
            return False
        else:
            if (x for x in axdef1 if x in axdef2):
                return True
            else:
                return False

    @staticmethod
    def _sect_is_subset(axdef1, axdef2):
        return set(axdef1).intersection(set(axdef2)) > 0

class Test(object):
    """
    The Quantipy Test object is a defined by a Link and the view name notation
    string of a counts or means view. All auxiliary figures needed to arrive
    at the test results are computed inside the instance of the object.
    """
    def __init__(self, link, view_name_notation, test_total=False):
        super(Test, self).__init__()
        # Infer whether a mean or proportion test is being performed
        view = link[view_name_notation]
        if view.meta()['agg']['method'] == 'descriptives':
            self.metric = 'means'
        else:
            self.metric = 'proportions'
        self.invalid = None
        self.no_pairs = None
        self.no_diffs = None
        self.parameters = None
        self.test_total = test_total
        self.mimic = None
        self.level = None
        # Calculate the required baseline measures for the test using the
        # Quantity instance
        self.Quantity = qp.Quantity(link, view.weights(), use_meta=True,
                                    base_all=self.test_total)
        self._set_baseline_aggregates(view)
        # Set information about the incoming aggregation
        # to be able to route correctly through the algorithms
        # and re-construct a Quantipy-indexed pd.DataFrame
        self.is_weighted = view.meta()['agg']['is_weighted']
        self.has_calc = view.has_calc()
        self.x = view.meta()['x']['name']
        self.xdef = view.dataframe.index.get_level_values(1).tolist()
        self.y = view.meta()['y']['name']
        self.ydef = view.dataframe.columns.get_level_values(1).tolist()
        columns_to_pair = ['@'] + self.ydef if self.test_total else self.ydef
        self.ypairs = list(combinations(columns_to_pair, 2))
        self.y_is_multi = view.meta()['y']['is_multi']
        self.multiindex = (view.dataframe.index, view.dataframe.columns)

    def __repr__(self):
        return ('%s, total included: %s, test metric: %s, parameters: %s, '
                'mimicked: %s, level: %s ')\
                % (Test, self.test_total, self.metric, self.parameters,
                   self.mimic, self.level)

    def _set_baseline_aggregates(self, view):
        """
        Derive or recompute the basic values required by the ``Test`` instance.
        """
        grps, exp, compl, calc, exclude, rescale = view.get_edit_params()
        if exclude is not None:
            self.Quantity.exclude(exclude)
        if self.metric == 'proportions' and self.test_total and view._has_code_expr():
            self.Quantity.group(grps, expand=exp, complete=compl)
        if self.metric == 'means':
            aggs = self.Quantity._dispersion(_return_mean=True,
                                             _return_base=True)
            self.sd, self.values, self.cbases = aggs[0], aggs[1], aggs[2]
            if not self.test_total:
                self.sd = self.sd[:, 1:]
                self.values = self.values[:, 1:]
                self.cbases = self.cbases[:, 1:]
        elif self.metric == 'proportions':
            if not self.test_total:
                self.values = view.dataframe.values.copy()
                self.cbases = view.cbases[:, 1:]
                self.rbases = view.rbases[1:, :]
                self.tbase = view.cbases[0, 0]
            else:
                agg = self.Quantity.count(margin=True, as_df=False)
                if calc is not None:
                    calc_only = view._kwargs.get('calc_only', False)
                    self.Quantity.calc(calc, axis='x', result_only=calc_only)
                self.values = agg.result[1:, :]
                self.cbases = agg.cbase
                self.rbases = agg.rbase[1:, :]
                self.tbase = agg.cbase[0, 0]

    def set_params(self, test_total=False, level='mid', mimic='Dim', testtype='pooled',
                   use_ebase=True, ovlp_correc=True, cwi_filter=False,
                   flag_bases=None):
        """
        Sets the test algorithm parameters and defines the type of test.

        This method sets the test's global parameters and derives the
        necessary measures for the computation of the test statistic.
        The default values correspond to the SPSS Dimensions Column Tests
        algorithms that control for bias introduced by weighting and
        overlapping samples in the column pairs of multi-coded questions.

        .. note:: The Dimensions implementation uses variance pooling.

        Parameters
        ----------
        test_total : bool, default False
            If set to True, the test algorithms will also include an existent
            total (@-) version of the original link and test against the
            unconditial data distribution.
        level : str or float, default 'mid'
            The level of significance given either as per 'low' = 0.1,
            'mid' = 0.05, 'high' = 0.01 or as specific float, e.g. 0.15.
        mimic : {'askia', 'Dim'} default='Dim'
            Will instruct the mimicking of a software specific test.
        testtype : str, default 'pooled'
            Global definition of the tests.
        use_ebase : bool, default True
            If True, will use the effective sample sizes instead of the
            the simple weighted ones when testing a weighted aggregation.
        ovlp_correc : bool, default True
            If True, will consider and correct for respondent overlap when
            testing between multi-coded column pairs.
        cwi_filter : bool, default False
            If True, will check an incoming count aggregation for cells that
            fall below a treshhold comparison aggregation that assumes counts
            to be independent.
        flag_bases : list of two int, default None
            If provided, the output dataframe will replace results that have
            been calculated on (eff.) bases below the first int with ``'**'``
            and mark results in columns with bases below the second int with
            ``'*'``

        Returns
        -------
        self
        """
        # Check if the aggregation is non-empty
        # and that there are >1 populated columns
        if np.nansum(self.values) == 0 or len(self.ydef) == 1:
            self.invalid = True
            if np.nansum(self.values) == 0:
                self.no_diffs = True
            if len(self.ydef) == 1:
                self.no_pairs = True
            self.mimic = mimic
            self.comparevalue, self.level = self._convert_level(level)
        else:
            # Set global test algorithm parameters
            self.invalid = False
            self.no_diffs = False
            self.no_pairs = False
            valid_mimics = ['Dim', 'askia']
            if mimic not in valid_mimics:
                raise ValueError('Failed to mimic: "%s". Select from: %s\n'
                                 % (mimic, valid_mimics))
            else:
                self.mimic = mimic
            if self.mimic == 'askia':
                self.parameters = {'testtype': 'unpooled',
                                   'use_ebase': False,
                                   'ovlp_correc': False,
                                   'cwi_filter': True,
                                   'base_flags': None}
                self.test_total = False
            elif self.mimic == 'Dim':
                self.parameters = {'testtype': 'pooled',
                                   'use_ebase': True,
                                   'ovlp_correc': True,
                                   'cwi_filter': False,
                                   'base_flags': flag_bases}
            self.level = level
            self.comparevalue, self.level = self._convert_level(level)
            # Get value differences between column pairings
            if self.metric == 'means':
                self.valdiffs = np.array(
                    [m1 - m2 for m1, m2 in combinations(self.values[0], 2)])
            if self.metric == 'proportions':
                # special to askia testing: counts-when-independent filtering
                if cwi_filter:
                    self.values = self._cwi()
                props = (self.values / self.cbases).T
                self.valdiffs = np.array([p1 - p2 for p1, p2
                                          in combinations(props, 2)]).T
            # Set test specific measures for Dimensions-like testing:
            # [1] effective base usage
            if use_ebase and self.is_weighted:
                if not self.test_total:
                    self.ebases = self.Quantity._effective_n(axis='x', margin=False)
                else:
                    self.ebases = self.Quantity._effective_n(axis='x', margin=True)
            else:
                self.ebases = self.cbases
            # [2] overlap correction
            if self.y_is_multi and self.parameters['ovlp_correc']:
                self.overlap = self._overlap()
            else:
                self.overlap = np.zeros(self.valdiffs.shape)
            # [3] base flags
            if flag_bases:
                self.flags = {'min': flag_bases[0],
                              'small': flag_bases[1]}
                self.flags['flagged_bases'] = self._get_base_flags()
            else:
                self.flags = None
        return self

    # -------------------------------------------------
    # Main algorithm methods to compute test statistics
    # -------------------------------------------------
    def run(self):
        """
        Performs the testing algorithm and creates an output pd.DataFrame.

        The output is indexed according to Quantipy's Questions->Values
        convention. Significant results between columns are presented as
        lists of integer y-axis codes where the column with the higher value
        is holding the codes of the columns with the lower values. NaN is
        indicating that a cell is not holding any sig. higher values
        compared to the others.
        """
        if not self.invalid:
            sigs = self.get_sig()
            return self._output(sigs)
        else:
            return self._empty_output()

    def get_sig(self):
        """
        TODO: implement returning tstats only.
        """
        stat = self.get_statistic()
        stat = self._convert_statistic(stat)
        if self.metric == 'means':
            diffs = pd.DataFrame(self.valdiffs, index=self.ypairs, columns=self.xdef).T
        elif self.metric == 'proportions':
            stat = pd.DataFrame(stat, index=self.xdef, columns=self.ypairs)
            diffs = pd.DataFrame(self.valdiffs, index=self.xdef, columns=self.ypairs)
        if self.mimic == 'Dim':
            return diffs[(diffs != 0) & (stat < self.comparevalue)]
        elif self.mimic == 'askia':
            return diffs[(diffs != 0) & (stat > self.comparevalue)]

    def get_statistic(self):
        """
        Returns the test statistic of the algorithm.
        """
        return self.valdiffs / self.get_se()

    def get_se(self):
        """
        Compute the standard error (se) estimate of the tested metric.

        The calculation of the se is defined by the parameters of the setup.
        The main difference is the handling of variances. **unpooled**
        implicitly assumes variance inhomogenity between the column pairing's
        samples. **pooled** treats variances effectively as equal.
        """
        if self.metric == 'means':
            if self.parameters['testtype'] == 'unpooled':
                return self._se_mean_unpooled()
            elif self.parameters['testtype'] == 'pooled':
                return self._se_mean_pooled()
        elif self.metric == 'proportions':
            if self.parameters['testtype'] == 'unpooled':
                return self._se_prop_unpooled()
            if self.parameters['testtype'] == 'pooled':
                return self._se_prop_pooled()

    # -------------------------------------------------
    # Conversion methods for levels and statistics
    # -------------------------------------------------
    def _convert_statistic(self, teststat):
        """
        Convert test statistics to match the decision rule of the test logic.

        Either transforms to p-values or returns the absolute value of the
        statistic, depending on the decision rule of the test.
        This is used to mimic other software packages as some tests'
        decision rules check test-statistic against pre-defined treshholds
        while others check sig. level against p-value.
        """
        if self.mimic == 'Dim':
            ebases_pairs = [eb1 + eb2 for eb1, eb2
                            in combinations(self.ebases[0], 2)]
            dof = ebases_pairs - self.overlap - 2
            dof[dof <= 1] = np.NaN
            return get_pval(dof, teststat)[1]
        elif self.mimic == 'askia':
            return abs(teststat)

    def _convert_level(self, level):
        """
        Determines the comparison value for the test's decision rule.

        Checks whether the level of test is a string that defines low, medium,
        or high significance or an "actual" level of significance and
        converts it to a comparison level/significance level tuple.
        This is used to mimic other software packages as some test's
        decision rules check test-statistic against pre-defined treshholds
        while others check sig. level against p-value.
        """
        if isinstance(level, (str, unicode)):
            if level == 'low':
                if self.mimic == 'Dim':
                    comparevalue = siglevel = 0.10
                elif self.mimic == 'askia':
                    comparevalue = 1.65
                    siglevel = 0.10
            elif level == 'mid':
                if self.mimic == 'Dim':
                    comparevalue = siglevel = 0.05
                elif self.mimic == 'askia':
                    comparevalue = 1.96
                    siglevel = 0.05
            elif level == 'high':
                if self.mimic == 'Dim':
                    comparevalue = siglevel = 0.01
                elif self.mimic == 'askia':
                    comparevalue = 2.576
                    siglevel = 0.01
        else:
            if self.mimic == 'Dim':
                comparevalue = siglevel = level
            elif self.mimic == 'askia':
                comparevalue = 1.65
                siglevel = 0.10

        return comparevalue, siglevel

    # -------------------------------------------------
    # Standard error estimates calculation methods
    # -------------------------------------------------
    def _se_prop_unpooled(self):
        """
        Estimated standard errors of prop. diff. (unpool. var.) per col. pair.
        """
        props = self.values/self.cbases
        unp_sd = ((props*(1-props))/self.cbases).T
        return np.array([np.sqrt(cat1 + cat2)
                         for cat1, cat2 in combinations(unp_sd, 2)]).T

    def _se_mean_unpooled(self):
        """
        Estimated standard errors of mean diff. (unpool. var.) per col. pair.
        """
        sd_base_ratio = self.sd / self.cbases
        return np.array([np.sqrt(sd_b_r1 + sd_b_r2)
                         for sd_b_r1, sd_b_r2
                         in combinations(sd_base_ratio[0], 2)])[None, :]

    def _se_prop_pooled(self):
        """
        Estimated standard errors of prop. diff. (pooled var.) per col. pair.

        Controlling for effective base sizes and overlap responses is
        supported and applied as defined by the test's parameters setup.
        """
        ebases_correc_pairs = np.array([1 / x + 1 / y
                                        for x, y
                                        in combinations(self.ebases[0], 2)])

        if self.y_is_multi and self.parameters['ovlp_correc']:
            ovlp_correc_pairs = ((2 * self.overlap) /
                                 [x * y for x, y
                                  in combinations(self.ebases[0], 2)])
        else:
            ovlp_correc_pairs = self.overlap

        counts_sum_pairs = np.array(
            [c1 + c2 for c1, c2 in combinations(self.values.T, 2)])
        bases_sum_pairs = np.expand_dims(
            [b1 + b2 for b1, b2 in combinations(self.cbases[0], 2)], 1)
        pooled_props = (counts_sum_pairs/bases_sum_pairs).T
        return (np.sqrt(pooled_props * (1 - pooled_props) *
                (np.array(ebases_correc_pairs - ovlp_correc_pairs))))

    def _se_mean_pooled(self):
        """
        Estimated standard errors of mean diff. (pooled var.) per col. pair.

        Controlling for effective base sizes and overlap responses is
        supported and applied as defined by the test's parameters setup.
        """
        ssw_base_ratios = self._sum_sq_w(base_ratio=True)
        enum = np.nan_to_num((self.sd ** 2) * (self.cbases-1))
        denom = self.cbases-ssw_base_ratios

        enum_pairs = np.array([enum1 + enum2
                               for enum1, enum2
                               in combinations(enum[0], 2)])
        denom_pairs = np.array([denom1 + denom2
                                for denom1, denom2
                                in combinations(denom[0], 2)])

        ebases_correc_pairs = np.array([1/x + 1/y
                                        for x, y
                                        in combinations(self.ebases[0], 2)])

        if self.y_is_multi and self.parameters['ovlp_correc']:
            ovlp_correc_pairs = ((2*self.overlap) /
                                 [x * y for x, y
                                  in combinations(self.ebases[0], 2)])
        else:
            ovlp_correc_pairs = self.overlap[None, :]

        return (np.sqrt((enum_pairs/denom_pairs) *
                        (ebases_correc_pairs - ovlp_correc_pairs)))

    # -------------------------------------------------
    # Specific algorithm values & test option measures
    # -------------------------------------------------
    def _sum_sq_w(self, base_ratio=True):
        """
        """
        if not self.Quantity.w == '@1':
            self.Quantity.weight()
        if not self.test_total:
            ssw = np.nansum(self.Quantity.matrix ** 2, axis=0)[[0], 1:]
        else:
            ssw = np.nansum(self.Quantity.matrix ** 2, axis=0)[[0], :]
        if base_ratio:
            return ssw/self.cbases
        else:
            return ssw

    def _cwi(self, threshold=5, as_df=False):
        """
        Derives the count distribution assuming independence between columns.
        """
        c_col_n = self.cbases
        c_cell_n = self.values
        t_col_n = self.tbase
        if self.rbases.shape[1] > 1:
            t_cell_n = self.rbases[1:, :]
        else:
            t_cell_n = self.rbases[0]
        np.place(t_col_n, t_col_n == 0, np.NaN)
        np.place(t_cell_n, t_cell_n == 0, np.NaN)
        np.place(c_col_n, c_col_n == 0, np.NaN)
        np.place(c_cell_n, c_cell_n == 0, np.NaN)
        cwi = (t_cell_n * c_col_n) / t_col_n
        cwi[cwi < threshold] = np.NaN
        if as_df:
            return pd.DataFrame(c_cell_n + cwi - cwi,
                                index=self.xdef, columns=self.ydef)
        else:
            return c_cell_n + cwi - cwi

    def _overlap(self):
        if self.is_weighted:
            self.Quantity.weight()
        m = self.Quantity.matrix.copy()
        m = np.nansum(m, 1) if self.test_total else np.nansum(m[:, 1:, 1:], 1)
        if not self.is_weighted:
            m /= m
        m[m == 0] = np.NaN
        col_pairs = list(combinations(range(0, m.shape[1]), 2))
        if self.parameters['use_ebase'] and self.is_weighted:
            # Overlap computation when effective base is being used
            w_sum_sq = np.array([np.nansum(m[:, [c1]] + m[:, [c2]], axis=0)**2
                                 for c1, c2 in col_pairs])
            w_sq_sum = np.array([np.nansum(m[:, [c1]]**2 + m[:, [c2]]**2, axis=0)
                        for c1, c2 in col_pairs])
            return np.nan_to_num((w_sum_sq/w_sq_sum)/2).T
        else:
            # Overlap with simple weighted/unweighted base size
            ovlp = np.array([np.nansum(m[:, [c1]] + m[:, [c2]], axis=0)
                             for c1, c2 in col_pairs])
            return (np.nan_to_num(ovlp)/2).T

    def _get_base_flags(self):
        bases = self.ebases[0]
        small = self.flags['small']
        minimum = self.flags['min']
        flags = []
        for base in bases:
            if base >= small:
                flags.append('')
            elif base < small and base >= minimum:
                flags.append('*')
            else:
                flags.append('**')
        return flags

    # -------------------------------------------------
    # Output creation
    # -------------------------------------------------
    def _output(self, sigs):
        res = {y: {x: [] for x in self.xdef} for y in self.ydef}
        test_columns = ['@'] + self.ydef if self.test_total else self.ydef
        for col, val in sigs.iteritems():
            if self._flags_exist():
                b1ix, b2ix = test_columns.index(col[0]), test_columns.index(col[1])
                b1_ok = self.flags['flagged_bases'][b1ix] != '**'
                b2_ok = self.flags['flagged_bases'][b2ix] != '**'
            else:
                b1_ok, b2_ok = True, True
            for row, v in val.iteritems():
                if v > 0:
                    if b2_ok:
                        if col[0] == '@':
                            res[col[1]][row].append('@H')
                        else:
                            res[col[0]][row].append(col[1])
                if v < 0:
                    if b1_ok:
                        if col[0] == '@':
                            res[col[1]][row].append('@L')
                        else:
                            res[col[1]][row].append(col[0])
        test = pd.DataFrame(res).applymap(lambda x: str(x))
        test = test.reindex(index=self.xdef, columns=self.ydef)
        if self._flags_exist():
           test = self._apply_base_flags(test)
           test.replace('[]*', '*', inplace=True)
        test.replace('[]', np.NaN, inplace=True)
        # removing test results on post-aggregation rows [calc()]
        if self.has_calc:
            if len(test.index) > 1:
                test.iloc[-1:, :] = np.NaN
            else:
                test.iloc[:, :] = np.NaN
        test.index, test.columns = self.multiindex[0], self.multiindex[1]
        return test

    def _empty_output(self):
        """
        """
        values = self.values
        if self.metric == 'proportions':
            if self.no_pairs or self.no_diffs:
                values[:] = np.NaN
            if values.shape == (1, 1) or values.shape == (1, 0):
                values = [np.NaN]
        if self.metric == 'means':
            if self.no_pairs:
                values = [np.NaN]
            if self.no_diffs and not self.no_pairs:
                values[:] = np.NaN
        return  pd.DataFrame(values,
                             index=self.multiindex[0],
                             columns=self.multiindex[1])
    def _flags_exist(self):
        return (self.flags is not None and
                not all(self.flags['flagged_bases']) == '')

    def _apply_base_flags(self, sigres, replace=True):
        flags = self.flags['flagged_bases']
        if self.test_total: flags = flags[1:]
        for res_col, flag in zip(sigres.columns, flags):
                if flag == '**':
                    if replace:
                        sigres[res_col] = flag
                    else:
                        sigres[res_col] = sigres[res_col] + flag
                elif flag == '*':
                    sigres[res_col] = sigres[res_col] + flag
        return sigres

class Nest(object):
    """
    Description of class...
    """
    def __init__(self, nest, data, meta):
        self.data = data
        self.meta = meta
        self.name = nest
        self.variables = nest.split('>')
        self.levels = len(self.variables)
        self.level_codes = []
        self.code_maps = None
        self._needs_multi = self._any_multicoded()

    def nest(self):
        self._get_nested_meta()
        self._get_code_maps()
        interlocked = self._interlock_codes()
        if not self.name in self.data.columns:
            recode_map = {code: intersection(code_pair) for code, code_pair
                          in enumerate(interlocked, start=1)}
            self.data[self.name] = np.NaN
            self.data[self.name] = recode(self.meta, self.data,
                                          target=self.name, mapper=recode_map)
        nest_info = {'variables': self.variables,
                     'level_codes': self.level_codes,
                     'levels': self.levels}
        return nest_info

    def _any_multicoded(self):
        return any(self.data[self.variables].dtypes == 'object')

    def _get_code_maps(self):
        code_maps = []
        for level, var in enumerate(self.variables):
            mapping = [{var: [int(code)]} for code
                       in self.level_codes[level]]
            code_maps.append(mapping)
        self.code_maps = code_maps
        return None

    def _interlock_codes(self):
        return list(product(*self.code_maps))

    def _get_nested_meta(self):
        meta_dict = {}
        qtext, valtexts = self._interlock_texts()
        meta_dict['type'] = 'delimited set' if self._needs_multi else 'single'
        meta_dict['text'] = {'en-GB': '>'.join(qtext[0])}
        meta_dict['values'] = [{'text' : {'en-GB': '>'.join(valtext)},
                                'value': c}
                               for c, valtext
                               in enumerate(valtexts, start=1)]
        self.meta['columns'][self.name] = meta_dict
        return None

    def _interlock_texts(self):
        all_valtexts = []
        all_qtexts = []
        for var in self.variables:
            var_valtexts = []
            values = self.meta['columns'][var]['values']
            all_qtexts.append(self.meta['columns'][var]['text'].values())
            for value in values:
                var_valtexts.append(value['text'].values()[0])
            all_valtexts.append(var_valtexts)
            self.level_codes.append([code['value'] for code in values])
        interlocked_valtexts = list(product(*all_valtexts))
        interlocked_qtexts = list(product(*all_qtexts))
        return interlocked_qtexts, interlocked_valtexts

class Multivariate(object):
    """
    An object that collects statistical algorithms, tools and functions.

    DESCP
    """
    def __init__(self, stack, data_key, filters=None):
        super(Multivariate, self).__init__()
        self.stack = stack
        self.data_key = data_key
        self.filter_def = 'no_filter' if filters is None else filters
        self.data = stack[data_key][self.filter_def].data
        self.prep = False
        self.analysis_data = None
        self.current_analysis = None
        self.link = None
        self.single_quantities = []
        self.cross_quantities = []
        self.w = None
        self.x = None
        self.y = None
        self.w = None

    def _validate_input_structure(self, analysis, x, y, w):
        """
        Check if provided x and y variables are valid for the analysis method.
        """
        one_x = len(x) == 1
        one_y = len(y) == 1
        invalid = False
        if analysis in ['correlation', 'covariance']:
            supported = ''
            pass
        elif analysis in ['correspondence', 'mass', 'chisq',
                          'expected_counts']:
            if not (one_x and one_y):
                invalid = True
            elif y[0] == '@':
                invalid = True
        elif analysis in ['turf', 'reach'] and y != ['@']:
            invalid = True
        if invalid:
            val_error = '"{}" analysis only supported on 1-on-1 relationships.'
            raise ValueError(val_error.format(analysis))

    def _prepare_analysis(self, analysis_name, x, y, w=None):
        """
        Create Quantity instances and set global analysis attributes.
        """
        if not self.prep:
            self.prep = True
            if y is None: y = '@'
            if not isinstance(x, list): x = [x]
            if not isinstance(y, list): y = [y]
            self._validate_input_structure(analysis_name, x, y, w)
            sets_meta = {analysis_name: {'x': x, 'y': y, 'w': w}}
            self.stack[self.data_key].meta['sets'].update({'multivariate': sets_meta})
            self.current_analysis = analysis_name
            self.x = x
            self.y = y
            self.w = w if w is not None else '@1'
            if self.y == ['@']:
                self.analysis_data = self.data[self.x + [self.w]]
            else:
                self.analysis_data = self.data[self.x + self.y + [self.w]]
            if not analysis_name in ['turf']:
                if self.y == ['@']: y = self.x
                for x, y in product(self.x, y):
                    cross_link = qp.Link(the_filter=self.filter_def, x=x, y=y,
                               data_key=self.data_key, stack=self.stack,
                               create_views=False)
                    self.cross_quantities.append(
                        qp.Quantity(cross_link, weight=self.w, use_meta=True))
                for x in self.x + self.y:
                    if x == '@':
                        pass
                    else:
                        single_link = qp.Link(the_filter=self.filter_def, x=x, y='@',
                                   data_key=self.data_key, stack=self.stack,
                                   create_views=False)
                        self.single_quantities.append(
                            qp.Quantity(single_link, weight=self.w, use_meta=True))
                if len(self.x) == 1 and len(self.y) == 1:
                    self.cross_quantities = self.cross_quantities[0]
                    self.single_quantities = self.single_quantities[0]

    def reach(self, items, base_reach_on=None):
        """
        Create a topline Reach analysis for an array of items.
        """
        self._prepare_analysis('reach', x=items, y=None, w=None)
        data = self.analysis_data.ix[:, :-1]
        topline = pd.concat([pd.DataFrame(data[col].value_counts(),
                                          columns=[col])
                             for col in data.columns], axis=1)
        drop_codes = [code for code in topline.index.tolist()
                      if code not in base_reach_on]
        max_reach = len(data.replace(drop_codes, np.NaN).dropna(how='all').index)
        max_reach = pd.DataFrame([max_reach, 100*float(max_reach)/len(data.index)],
                                 columns=['Reach']).T
        freqs = topline.ix[base_reach_on, :].sum()
        freqs = pd.concat([freqs, freqs.div(len(data.index))*100], axis=1)
        freqs = pd.concat([freqs, max_reach], axis=0)
        freqs.columns = ['n', '%']
        return freqs

    def turf(self, items, max_comb=None, base_reach_on=None):
        """
        Run a Total Unduplicated Reach and Frequency model.
        """
        pass

    def _show_full_matrix(self):
        return self.y == ['@']

    def _format_output_pairs(self, nparray):
        if self._show_full_matrix():
            return nparray.reshape(len(self.x), len(self.x))
        else:
            return nparray.reshape(len(self.x), len(self.y))

    def _format_result_df(self, nparray):
        names = [self.current_analysis, 'Questions']
        if self._show_full_matrix():
            index = self.x
            columns = index
        else:
            index = self.x
            columns = self.y
        return pd.DataFrame(nparray, index=index, columns=columns)

    def _make_index_pairs(self):
        full_range = len(self.x + self.y) - 1
        x_range = range(0, len(self.x))
        y_range = range(x_range[-1] + 1, full_range + 1)
        if self._show_full_matrix():
            return list(product(range(0, full_range), repeat=2))
        else:
            return list(product(x_range, y_range))

    def mass(self, x, y, w=None, margin=None):
        """
        Compute rel. margins or total cell frequencies of a contigency table.
        """
        self._prepare_analysis('mass', x, y, w)
        counts = self.cross_quantities.count(margin=False)
        total = counts.cbase[0, 0]
        if margin is None:
            return counts.result.values / total
        elif margin == 'x':
            return  counts.rbase[1:, :] / total
        elif margin == 'y':
            return  (counts.cbase[:, 1:] / total).T

    def expected_counts(self, x, y, w=None, return_observed=False):
        """
        Compute expected cell distribution given observed absolute frequencies.
        """
        self._prepare_analysis('expected_counts', x, y, w)
        counts = self.cross_quantities.count(margin=False)
        total = counts.cbase[0, 0]
        row_m = counts.rbase[1:, :]
        col_m = counts.cbase[:, 1:]
        if not return_observed:
            return (row_m * col_m) / total
        else:
            return counts.result.values, (row_m * col_m) / total

    def chi_sq(self, x, y, w=None, as_inertia=False):
        """
        Compute global Chi^2 statistic, optionally transformed into Inertia.
        """
        self._prepare_analysis('chisq', x, y, w)
        obs, exp = self.expected_counts(x=x, y=y, return_observed=True)
        diff_matrix = ((obs - exp)**2) / exp
        total_chi_sq = np.nansum(diff_matrix)
        if not as_inertia:
            return total_chi_sq
        else:
            return total_chi_sq / np.nansum(obs)

    def cov(self, x, y, w=None, n=False, as_df=True):
        """
        Compute the sample covariance (matrix).
        """
        self._prepare_analysis('covariance', x, y, w)
        full_matrix = self._show_full_matrix()
        pairs = self._make_index_pairs()
        d = self.analysis_data
        means = [q.summarize('mean', margin=False, as_df=False).result[0, 0]
                 for q in self.single_quantities]
        m_diff = d - (means + [0.0])
        unbiased_n = [np.nansum(d.ix[:, [ix1, ix2, -1]].dropna().ix[:, -1]) - 1
                      for ix1, ix2 in pairs]
        cross_prods = [np.nansum(m_diff.ix[:, -1] *
                                 m_diff.ix[:, ix1] *
                                 m_diff.ix[:, ix2])
                       for ix1, ix2 in pairs]
        cov = np.array(cross_prods) / unbiased_n
        if n:
            paired_n = [n + 1 for n in unbiased_n]
        if as_df:
            cov_result = self._format_result_df(self._format_output_pairs(cov))
        else:
            cov_result = self._format_output_pairs(cov)
        if n:
            return paired_n, cov_result
        else:
            return cov_result

    def _mass_std_weights(self):
        counts = [cq.count(margin=False).result
                  for cq in self.cross_quantities]
        mass_coords = [list(product(c.index.get_level_values(1),
                                    c.columns.get_level_values(1)))
                       for c in counts]
        mass_w = [(c/c.values.sum().sum() * 1000) for c in counts]
        for mw in mass_w:
            mw.index, mw.columns = mw.index.droplevel(), mw.columns.droplevel()
        x, y = self.x, self.y if not self.y == ['@'] else self.x
        data = self.analysis_data.copy()
        var_combs = list(product(x, y))
        comb_vars_names = ['x'.join(var_comb) for var_comb in var_combs]
        for comb_no, comb_vars in enumerate(var_combs):
            comb_var = comb_vars_names[comb_no]
            data[comb_var] = np.NaN
            for mass_coord in mass_coords[comb_no]:
                coord_idx = data[(data[comb_vars[0]]==mass_coord[0]) &
                                 (data[comb_vars[1]]==mass_coord[1])].index
                coord_value = mass_w[comb_no].loc[mass_coord[0],
                                                  mass_coord[1]]
                data.loc[coord_idx, comb_var] = coord_value
        weights = [data[mw].dropna().values.flatten().tolist()
                   for mw in comb_vars_names]
        return weights

    def corr(self, x, y, w=None, scatter=True, sigs=False, n=False, as_df=True):
        """
        Generate the sample Pearson correlation coeffcients (matrix).

        Also able to generate scatter plots related to the variable pairs: data
        points of categorical variables will be mass-standardized to reflect
        contigency table frequencies.
        """
        self._prepare_analysis('correlation', x, y, w=w)
        full_matrix = self._show_full_matrix()
        pairs = self._make_index_pairs()
        cov = self.cov(x=x, y=y, w=w, n=n, as_df=False)
        if n:
            ns, cov = cov[0], cov[1].flatten()
        else:
            cov = cov.flatten()
        stddev = [q.summarize('stddev', margin=False, as_df=False).result[0, 0]
                  for q in self.single_quantities]
        normalizer = [stddev[ix1] * stddev[ix2] for ix1, ix2 in pairs]
        corrs = cov / normalizer

        corr_df = self._format_result_df(self._format_output_pairs(corrs))
        pal = sns.blend_palette(["lightgrey", "red"], as_cmap=True)
        corr_res = sns.heatmap(corr_df, annot=True, cbar=None, fmt='.2f',
                         square=True, robust=True, cmap=pal,
                         center=np.mean(corr_df.values), linewidth=0.5)
        fig = corr_res.get_figure()
        fig.savefig('C:/Users/alt/Desktop/Bugs and testing/MENA CA/test2.png')


        stdizers = self._mass_std_weights()
        sns.set_style('dark')
        sns.set_context('paper')
        data = self.analysis_data[:-1]
        x, y = self.x, self.y if self.y != ['@'] else self.x
        plot = sns.pairplot(data, dropna=True, x_vars=y, y_vars=x,
                            diag_kind=None, kind=None)
        subplots = plot.fig.get_axes()
        for corr, n, ax, pair, stdizer in zip(corrs, ns, subplots, pairs, stdizers):
            ax.set_title('pearson={} (N={})'.format(np.round(corr, 2), int(np.round(n, 0))))
            ax.scatter(x=data.iloc[:, pair[1]], y=data.iloc[:, pair[0]],
                       s=stdizer,edgecolor='w', marker='o', c='r')
        #plot.fig.get_axes()[-1] = (test.get_figure())

        plot.fig.subplots_adjust(top=0.9)
        plot.fig.suptitle('Scatterplots\n-mass-standarized-', fontsize=12)

        plot.savefig('C:/Users/alt/Desktop/Bugs and testing/MENA CA/check.png')

        if as_df:
            corr = self._format_result_df(self._format_output_pairs(corrs))
        else:
            corr = self._format_output_pairs(corrs)
        return corr

    def correspondence(self, x, y, w=None, norm='sym', summary=True, plot=False):
        """
        Perform a (multiple) correspondence analysis.

        Parameters
        ----------
        norm : {'sym', 'princ'}, default 'sym'
            <DESCP>
        summary : bool, default True
            If True, the output will contain a dataframe that summarizes core
            information about the Inertia decomposition.
        plot : bool, default False
            If set to True, a correspondence map plot will be saved in the
            Stack's data path location.
        Returns
        -------
        results: pd.DataFrame
            Summary of analysis results.
        """
        self._prepare_analysis('correspondence', x, y, weight)
        # 1. Chi^2 analysis
        obs, exp = self.expected_counts(x=x, y=y, return_observed=True)
        chisq = self.chi_sq(x=x, y=y)
        inertia = chisq / np.nansum(obs)
        # 2. svd on standardized residuals
        std_residuals = ((obs - exp) / np.sqrt(exp)) / np.sqrt(np.nansum(obs))
        sv, row_eigen_mat, col_eigen_mat, ev = self._svd(std_residuals)
        # 3. row and column coordinates
        a = 0.5 if norm == 'sym' else 1.0
        row_mass = self.mass(x=x, y=y, margin='x')
        col_mass = self.mass(x=x, y=y, margin='y')
        dim = min(row_mass.shape[0]-1, col_mass.shape[0]-1)
        row_sc = (row_eigen_mat * sv[:, 0] ** a) / np.sqrt(row_mass)
        col_sc = (col_eigen_mat.T * sv[:, 0] ** a) / np.sqrt(col_mass)
        if plot:
            # prep coordinates for plot
            item_sep = len(self.data.xdef)
            dim1_c = [r_s[0] for r_s in row_sc] + [c_s[0] for c_s in col_sc]
            dim2_c = [r_s[1] for r_s in row_sc] + [c_s[1] for c_s in col_sc]
            dim1_xitem, dim2_xitem = dim1_c[:item_sep+1], dim2_c[:item_sep+1]
            dim1_yitem, dim2_yitem = dim1_c[item_sep:], dim2_c[item_sep:]
            coords = {'x': [dim1_xitem, dim2_xitem],
                      'y': [dim1_yitem, dim2_yitem]}
            self.plot('CA', coords)
        if summary:
            # core results summary table
            _dim = xrange(1, dim+1)
            _chisq = ([np.NaN] * (dim-1)) + [chisq]
            _sv, _ev = sv[:dim, 0], ev[:dim, 0]
            _expl_inertia = 100 * (ev[:dim, 0] / inertia)
            _cumul_expl_inertia = np.cumsum(_expl_inertia)
            _perc_chisq = _expl_inertia / 100 * chisq
            labels = ['Dimension', 'Total Chi^2', 'Singular values', 'Eigen values',
                     'explained % of Inertia', 'cumulative % explained',
                     'explained Chi^2']
            results = pd.DataFrame([_dim, _chisq, _sv, _ev, _expl_inertia,
                                    _cumul_expl_inertia,_perc_chisq]).T
            results.columns = labels
            results.set_index('Dimension', inplace=True)
            return results

    def _svd(self, matrix, return_eigen_matrices=True, return_eigen=True):
        """
        Singular value decomposition wrapping np.linalg.svd().
        """
        u, s, v = np.linalg.svd(matrix, full_matrices=False)
        s = s[:, None]
        if not return_eigen:
            if return_eigen_matrices:
                return s, u, v
            else:
                return s
        else:
            if return_eigen_matrices:
                return s, u, v, (s ** 2)
            else:
                return s, (s ** 2)

    def plot(self, type, point_coords):
        plt.set_autoscale_on = False
        plt.figure(figsize=(10, 10))
        if type == 'CA':
            plt.suptitle('Correspondence map\n-Symmetrical biplot-',
                         fontsize=14, fontweight='bold')
            plt.xlim([-1, 1])
            plt.ylim([-1, 1])
            plt.axvline(x=0.0, c='k', ls='solid')
            plt.axhline(y=0.0, c='k', ls='solid')
            plt.scatter(point_coords['x'][0], point_coords['x'][1],
                        c='r', marker='^', s=40)
            plt.scatter(point_coords['y'][0], point_coords['y'][1],
                        s=40)
            label_map = self._get_point_label_map('CA', point_coords)
            for axis in label_map.keys():
                for lab, coord in label_map[axis].items():
                    plt.annotate(lab, coord, fontsize=10)

            plt.savefig('C:/Users/alt/Desktop/Bugs and testing/MENA CA/test.pdf')

    def set_plot_options(self, option, value):
        """
        """
        plot_options = {
            'val_labels_in_legend': False,
        }

    def _get_point_label_map(self, type, point_coords):
        if type == 'CA':
            xcoords = zip(point_coords['x'][0],point_coords['x'][1])
            xlabels = self.data._get_response_texts(self.data.x)
            x_point_map = {lab: coord for lab, coord in zip(xlabels, xcoords)}
            ycoords = zip(point_coords['y'][0], point_coords['y'][1])
            ylabels = self.data._get_response_texts(self.data.y)
            y_point_map = {lab: coord for lab, coord in zip(ylabels, ycoords)}
            return {'x': x_point_map, 'y': y_point_map}


##############################################################################
##############################################################################
##############################################################################

class Cache(defaultdict):


    def __init__(self):
        # The 'lock_cache' raises an exception in the
        super(Cache, self).__init__(Cache)

    def __reduce__(self):
        return self.__class__, tuple(), None, None, self.iteritems()


    def set_obj(self, collection, key, obj):
        '''
        Save a Quantipy resource inside the cache.

        Parameters
        ----------
        collection : {'matrices', 'weight_vectors', 'quantities',
                      'mean_view_names', 'count_view_names'}
            The key of the collection the object should be placed in.
        key : str
            The reference key for the object.
        obj : Specific Quantipy or arbitrary Python object.
            The object to store inside the cache.

        Returns
        -------
        None
        '''
        self[collection][key] = obj

    def get_obj(self, collection, key):
        '''
        Look up if an object exists in the cache and return it.

        Parameters
        ----------
        collection : {'matrices', 'weight_vectors', 'quantities',
                      'mean_view_names', 'count_view_names'}
            The key of the collection to look into.
        key : str
            The reference key for the object.

        Returns
        -------
        obj : Specific Quantipy or arbitrary Python object.
            The cached object mapped to the passed key.
        '''
        if collection == 'matrices':
            return self[collection].get(key, (None, None))
        elif collection == 'squeezed':
            return self[collection].get(key, (None, None, None, None, None, None, None))
        else:
            return self[collection].get(key, None)

##############################################################################
##############################################################################
##############################################################################

class DataSet(object):
    """
    A set of casedata (required) and meta data (optional).

    DESC.
    """
    def __init__(self, name):
        self.path = None
        self.name = name
        self.filtered = 'no_filter'
        self._data = None
        self._meta = None
        self._tk = None
        self._cache = Cache()

    # ------------------------------------------------------------------------
    # ITEM ACCESS / OVERRIDING
    # ------------------------------------------------------------------------
    def __getitem__(self, var):
        if isinstance(var, (unicode, str)):
            if not self._is_array(var):
                return self._data[var]
            else:
                items = self._get_itemmap(var, non_mapped='items')
                return self._data[items]
        else:
            return self._data[var]

    # ------------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------------
    def read(self, path_data, path_meta):
        self._data = qp.dp.io.load_csv(path_data+'.csv')
        self._meta = qp.dp.io.load_json(path_meta+'.json')
        self.path = '/'.join(path_data.split('/')[:-1])
        self._tk = self._meta['lib']['default text']
        self._data['@1'] = np.ones(len(self._data))
        self._data.index = list(xrange(0, len(self._data.index)))

    def data(self):
        return self._data

    def meta(self):
        return self._meta

    def cache(self):
        return self._cache

    # ------------------------------------------------------------------------
    # META INSPECTION/MANIPULATION/HANDLING
    # ------------------------------------------------------------------------
    def set_missings(self, var, missing_map=None):
        if missing_map is None:
            missing_map = {}
        if any(isinstance(k, tuple) for k in missing_map.keys()):
            flat_missing_map = {}
            for miss_code, miss_type in missing_map.items():
                if isinstance(miss_code, tuple):
                    for code in miss_code:
                        flat_missing_map[code] = miss_type
                else:
                    flat_missing_map[miss_code] = miss_type
            missing_map = flat_missing_map
        if self._is_array(var):
            var = self._get_itemmap(var, non_mapped='items')
        else:
            if not isinstance(var, list): var = [var]
        for v in var:
            if self._has_missings(v):
                self.meta()['columns'][v].update({'missings': missing_map})
            else:
                self.meta()['columns'][v]['missings'] = missing_map

    def _get_missings(self, var):
        if self._is_array(var):
            var = self._get_itemmap(var, non_mapped='items')
        else:
            if not isinstance(var, list): var = [var]
        for v in var:
            if self._has_missings(v):
                return self.meta()['columns'][v]['missings']
            else:
                return None

    def describe(self, var=None, restrict_to=None, text_key=None):
        """
        Inspect the DataSet's global or variable level structure.
        """
        if text_key is None: text_key = self._tk
        if var is not None:
            return self._get_meta(var, restrict_to, text_key)
        if self._meta['columns'] is None:
            return 'No meta attached to data_key: %s' %(data_key)
        else:
            types = {
                'int': [],
                'float': [],
                'single': [],
                'delimited set': [],
                'string': [],
                'date': [],
                'time': [],
                'array': [],
                'N/A': []
            }
            not_found = []
            for col in self._data.columns:
                if not col in ['@1', 'id_L1', 'id_L1.1']:
                    try:
                        types[
                              self._meta['columns'][col]['type']
                             ].append(col)
                    except:
                        types['N/A'].append(col)
            for mask in self._meta['masks'].keys():
                types[self._meta['masks'][mask]['type']].append(mask)
            idx_len = max([len(t) for t in types.values()])
            for t in types.keys():
                typ_padded = types[t] + [''] * (idx_len - len(types[t]))
                types[t] = typ_padded
            types = pd.DataFrame(types)
            types.columns.name = 'size: {}'.format(len(self._data))
            if restrict_to:
                types = pd.DataFrame(types[restrict_to]).replace('', np.NaN)
                types = types.dropna()
                types.columns.name = 'count: {}'.format(len(types))
            return types

    def _get_type(self, var):
        if var in self._meta['masks'].keys():
            return self._meta['masks'][var]['type']
        else:
             return self._meta['columns'][var]['type']

    def _has_missings(self, var):
        return 'missings' in self.meta()['columns'][var].keys()

    def _is_numeric(self, var):
        return self._get_type(var) in ['float', 'int']

    def _is_array(self, var):
        return self._get_type(var) == 'array'

    def _is_multicode_array(self, mask_element):
        return self[mask_element].dtype == 'object'

    def _get_label(self, var, text_key=None):
        if text_key is None: text_key = self._tk
        if self._get_type(var) == 'array':
            return self._meta['masks'][var]['text'][text_key]
        else:
            return self._meta['columns'][var]['text'][text_key]

    def _get_valuemap(self, var, text_key=None, non_mapped=None):
        if text_key is None: text_key = self._tk
        if self._get_type(var) == 'array':
            vals = self._meta['lib']['values'][var]
        else:
            vals = emulate_meta(self._meta,
                                self._meta['columns'][var].get('values', None))
        if non_mapped in ['codes', 'lists', None]:
            codes = [v['value'] for v in vals]
            if non_mapped == 'codes':
                return codes
        if non_mapped in ['texts', 'lists', None]:
            texts = [v['text'][text_key] for v in vals]
            if non_mapped == 'texts':
                return texts
        if non_mapped == 'lists':
            return codes, texts
        else:
            return zip(codes, texts)

    def _get_itemmap(self, var, text_key=None, non_mapped=None):
        if text_key is None: text_key = self._tk
        if non_mapped in ['items', 'lists', None]:
            items = [i['source'].split('@')[-1]
                     for i in self._meta['masks'][var]['items']]
            if non_mapped == 'items':
                return items
        if non_mapped in ['texts', 'lists', None]:
            items_texts = [self._meta['columns'][i]['text'][text_key]
                           for i in items]
            if non_mapped == 'texts':
                return items_texts
        if non_mapped == 'lists':
            return items, items_texts
        else:
            return zip(items, items_texts)

    def _get_meta(self, var, restrict_to=None,  text_key=None):
        if text_key is None: text_key = self._tk
        var_type = self._get_type(var)
        label = self._get_label(var, text_key)
        missings = self._get_missings(var)
        if not self._is_numeric(var):
            codes, texts = self._get_valuemap(var, non_mapped='lists')
            if missings:
                missings = [None if code not in missings else missings[code]
                            for code in codes]
            else:
                missings = [None] * len(codes)
            if var_type == 'array':
                items, items_texts = self._get_itemmap(var, non_mapped='lists')
                idx_len = max((len(codes), len(items)))
                if len(codes) > len(items):
                    pad = (len(codes) - len(items))
                    items = self._pad_meta_list(items, pad)
                    items_texts = self._pad_meta_list(items_texts, pad)
                elif len(codes) < len(items):
                    pad = (len(items) - len(codes))
                    codes = self._pad_meta_list(codes, pad)
                    texts = self._pad_meta_list(texts, pad)
                    missings = self._pad_meta_list(missings, pad)
                elements = [items, items_texts, codes, texts, missings]
                columns = ['items', 'item texts', 'codes', 'texts', 'missing type']
            else:
                idx_len = len(codes)
                elements = [codes, texts, missings]
                columns = ['codes', 'texts', 'missing type']
            meta_s = [pd.Series(element, index=range(0, idx_len))
                      for element in elements]
            meta_df = pd.concat(meta_s, axis=1)
            meta_df.columns = columns
            meta_df.index.name = var_type
            meta_df.columns.name = '{}: {}'.format(var, label)
        else:
            meta_df = pd.DataFrame(['N/A'])
            meta_df.index = [var_type]
            meta_df.columns = ['{}: {}'.format(var, label)]
        return meta_df

    @staticmethod
    def _pad_meta_list(meta_list, pad_to_len):
        return meta_list + ([''] * pad_to_len)

    # ------------------------------------------------------------------------
    # DATA MANIPULATION/HANDLING
    # ------------------------------------------------------------------------
    def make_dummy(self, var):
        if not self._is_array(var):
            if self[var].dtype == 'object': # delimited set-type data
                dummy_data = self[var].str.get_dummies(';')
                if self.meta is not None:
                    var_codes = self._get_valuemap(var, non_mapped='codes')
                    dummy_data.columns = [int(col) for col in dummy_data.columns]
                    dummy_data = dummy_data.reindex(columns=var_codes)
                    dummy_data.replace(np.NaN, 0, inplace=True)
                if self.meta:
                    dummy_data.sort_index(axis=1, inplace=True)
            else: # single, int, float data
                dummy_data = pd.get_dummies(self[var])
                if self.meta and not self._is_numeric(var):
                    var_codes = self._get_valuemap(var, non_mapped='codes')
                    dummy_data = dummy_data.reindex(columns=var_codes)
                    dummy_data.replace(np.NaN, 0, inplace=True)
                dummy_data.rename(
                    columns={
                        col: int(col)
                        if float(col).is_integer()
                        else col
                        for col in dummy_data.columns
                    },
                    inplace=True)
        else: # array-type data
            items = self._get_itemmap(var, non_mapped='items')
            codes = self._get_valuemap(var, non_mapped='codes')
            dummy_data = []
            if self._is_multicode_array(items[0]):
                for i in items:
                    i_dummy = self[i].str.get_dummies(';')
                    i_dummy.columns = [int(col) for col in i_dummy.columns]
                    dummy_data.append(i_dummy.reindex(columns=codes))
            else:
                for i in items:
                    dummy_data.append(
                        pd.get_dummies(self[i]).reindex(columns=codes))
            dummy_data = pd.concat(dummy_data, axis=1)
            cols = ['{}_{}'.format(i, c) for i in items for c in codes]
            dummy_data.columns = cols
        return dummy_data

    def code_count(self, var, ignore=None, total=None):
        data = self.make_dummy(var)
        is_array = self._is_array(var)
        if ignore:
            if ignore == 'meta': ignore = self._get_missings(var).keys()
            if is_array:
                ignore = [col for col in data.columns for i in ignore
                          if col.endswith(str(i))]
            slicer = [code for code in data.columns if code not in ignore]
            data = data[slicer]
        if total:
            return data.sum().sum()
        else:
            if is_array:
                items = self._get_itemmap(var, non_mapped='items')
                data = pd.concat([data[[col for col in data.columns
                                        if col.startswith(item)]].sum(axis=1)
                                  for item in items], axis=1)
                data.columns = items
            else:
                data = pd.DataFrame(data.sum(axis=0))
                data.columns = [var]
            return data

    def filter(self, alias, condition, inplace=False):
        """
        Filter the DataSet using a Quantipy logical expression.
        """
        if not inplace:
            data = self._data.copy()
        else:
            data = self._data
        filter_idx = get_logic_index(pd.Series(data.index), condition, data)
        filtered_data = data.iloc[filter_idx[0], :]
        if inplace:
            self.filtered = alias
            self._data = filtered_data
        else:
            new_ds = DataSet(self.name)
            new_ds._data = filtered_data
            new_ds._meta = self._meta
            new_ds.filtered = alias
            return new_ds

    # ------------------------------------------------------------------------
    # LINK OBJECT CONVERSION & HANDLERS
    # ------------------------------------------------------------------------
    def link(self, filters=None, x=None, y=None, views=None):
        """
        Create a Link instance from the DataSet.
        """
        if filters is None: filters = 'no_filter'
        l = Link(self, filters, x, y)
        return l

##############################################################################

class Link(Quantity, dict):
    def __init__(self, ds, filters=None, x=None, y=None, views=None):
        self.ds_key = ds.name
        self.filters = filters
        self.x = x
        self.y = y
        self.id = '[{}][{}][{}][{}]'.format(self.ds_key, self.filters, self.x,
                                            self.y)
        self.stack_connection = False
        self.quantified = False
        self._quantify(ds)
        #---------------------------------------------------------------------

    def _clear(self):
        ds_key, filters, x, y = self.ds_key, self.filters, self.x, self.y
        _id, stack_connection = self.id, self.stack_connection
        dataset, data, meta, cache = self.dataset, self.data, self.meta, self.cache
        self.__dict__.clear()
        self.ds_key, self.filters, self.x, self.y = ds_key, filters, x, y
        self.id, self.stack_connection = _id, stack_connection
        return None

    def _quantify(self, ds):
        # Establish connection to source dataset components when in Stack-mode
        def dataset():
            """
            Ensure a Link is able to track back to its orignating dataset.
            """
            return ds
        def data():
            """
            Ensure a Link is able to track back to its orignating case data.
            """
            return ds.data()
        def meta():
            """
            Ensure a Link is able to track back to its orignating meta data.
            """
            return ds.meta()
        def cache():
            """
            Ensure a Link is able to track back to its cached data vectors.
            """
            return ds.cache()
        self.dataset = dataset
        self.data = data
        self.meta = meta
        self.cache = cache
        Quantity.__init__(self, self)
        return None

#     def __repr__(self):
#         info = 'Link - id: {}\nquantified: {} | stack connected: {} | views: {}'
#         return info.format(self.id, self.quantified, self.stack_connection,
#                            len(self.values()))

    def describe(self):
        described = pd.Series(self.keys(), name=self.id)
        described.index.name = 'views'
        return described

##############################################################################

class Stack(defaultdict):
    def __init__(self, name=''):
        super(Stack, self).__init__(Stack)
        self.name = name
        self.ds = None

    # ====================================================================
    # THESE NEED TO GET A REVIEW!
    # ====================================================================
    # def __reduce__(self):
    #     arguments = (self.name, )
    #     states = self.__dict__.copy()
    #     if states['ds'] is not None:
    #         states['ds'].__dict__['_cache'] = Cache()
    #     return self.__class__, arguments, states, None, self.iteritems()

    # ====================================================================
    # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    # ====================================================================

    # ------------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------------
    def add_dataset(self, dataset):
        self.ds = dataset

    def save(self, path_stack, compressed=False):
        if compressed:
            f = gzip.open(path_stack, 'wb')
        else:
            f = open(path_stack, 'wb')
        dill.dump(self, f)
        f.close()
        return None

    def load(self, path_stack, compressed=False):
        if compressed:
            f = gzip.open(path_stack, 'rb')
        else:
            f = open(path_stack, 'rb')
        loaded_stack = dill.load(f)
        f.close()
        return loaded_stack

    # ------------------------------------------------------------------------
    # DATA/LINK POPULATION
    # ------------------------------------------------------------------------
    def refresh(self):
        pass

    def populate(self, filters=None, x=None, y=None, weights=None, views=None):
        """
        Populate the Stack instance with Links that (optionally) hold Views.
        """
        if filters is None: filters = ['no_filter']
        for _filter in filters:
            for _x in x:
                for _y in y:
                    if not isinstance(self[self.ds.name][_filter][_x][_y], Link):
                        l = self.ds.link(_filter, _x, _y)
                        l.stack_connection = True
                        self[self.ds.name][_filter][_x][_y] = l
                    else:
                        l = self.get(self.ds.name, _filter, _x, _y)
                        l.stack_connection = True
                    if views is not None:
                        if not isinstance(views, ViewMapper):
                            # Use DefaultViews if no view were given
                            if views is None:
                                pass
                            elif isinstance(views, (list, tuple)):
                                views = QuantipyViews(views=views)
                            else:
                                print 'ERROR - VIEWS CRASHED!'
                        views._apply_to(l, weights)
                        l._clear()

    # ------------------------------------------------------------------------
    # INSPECTION & QUERY
    # ------------------------------------------------------------------------
    def get(self, ds_key=None, filters=None, x=None, y=None):
        """
        Return Link from Stack.
        """
        if ds_key is None and len(self.keys()) > 1:
            key_err = 'Cannot select from multiple datasets when no key is provided.'
            raise KeyError(key_err)
        elif ds_key is None and len(self.keys()) == 1:
            ds_key = self.keys()[0]
        if filters is None: filters = 'no_filter'
        if not isinstance(self[ds_key][filters][x][y], Link):
            l = Link(self.ds, filters, x, y)
        else:
            l = self[ds_key][filters][x][y]
            l._quantify(self.ds)
        return l

    def describe(self, index=None, columns=None, query=None, split_view_names=False):
        """
        Generates a structured overview of all Link defining Stack elements.

        Parameters
        ----------
        index, columns : str of or list of {'data', 'filter', 'x', 'y', 'view'},
                         optional
            Controls the output representation by structuring a pivot-style
            table according to the index and column values.
        query : str
            A query string that is valid for the pandas.DataFrame.query() method.
        split_view_names : bool, default False
            If True, will create an output of unique view name notations split
            up into their components.

        Returns
        -------
        description : pandas.DataFrame
            DataFrame summing the Stack's structure in terms of Links and Views.
        """
        stack_tree = []
        for dk in self.keys():
            path_dk = [dk]
            filters = self[dk]

#             for fk in filters.keys():
#                 path_fk = path_dk + [fk]
#                 xs = self[dk][fk]

            for fk in filters.keys():
                path_fk = path_dk + [fk]
                xs = self[dk][fk]

                for sk in xs.keys():
                    path_sk = path_fk + [sk]
                    ys = self[dk][fk][sk]

                    for tk in ys.keys():
                        path_tk = path_sk + [tk]
                        views = self[dk][fk][sk][tk]

                        if views.keys():
                            for vk in views.keys():
                                path_vk = path_tk + [vk, 1]
                                stack_tree.append(tuple(path_vk))
                        else:
                            path_vk = path_tk + ['|||||', 1]
                            stack_tree.append(tuple(path_vk))

        column_names = ['data', 'filter', 'x', 'y', 'view', '#']
        description = pd.DataFrame.from_records(stack_tree, columns=column_names)
        if split_view_names:
            views_as_series = pd.DataFrame(
                description.pivot_table(values='#', columns='view', aggfunc='count')
                ).reset_index()['view']
            parts = ['xpos', 'agg', 'condition', 'rel_to', 'weights',
                     'shortname']
            description = pd.concat(
                (views_as_series,
                 pd.DataFrame(views_as_series.str.split('|').tolist(),
                              columns=parts)), axis=1)

        description.replace('|||||', np.NaN, inplace=True)
        if query is not None:
            description = description.query(query)
        if not index is None or not columns is None:
            description = description.pivot_table(values='#', index=index, columns=columns,
                                aggfunc='count')
        return description