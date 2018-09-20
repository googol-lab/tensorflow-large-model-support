# (C) Copyright IBM Corp. 2018. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""LMS
"""
from tensorflow.python.platform import tf_logging
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops

import time
from six.moves import queue as Queue
from lms import topos
from lms import util as ut


class LMS(object):
    """LMS class for Large Model Support (LMS).

    The `LMS` object statically modifies a model by swapping its tensors
    to the host so that the model can be trained with the limited memory
    of GPUs.

    Tensors those are generated by forward operations and consumed by
    backward operations are candidates for swapping. The `LMS` object will
    automatically find these tensors.

    Swapping is done by cutting the link between two operations whose
    topological-sort distance between them is greater than a given
    `threshold`, then replacing the link by inserting `identity`
    operations on the host. In theory, this procedure does not have any
    effect on the training convergence as well as inference task.
    """
    def __init__(self, graph=None,
                 excl_scopes=set(),
                 incl_scopes=set(),
                 excl_types=set(),
                 incl_types=set(),
                 swapout_threshold=-1,
                 swapin_groupby=0,
                 swapin_ahead=-1,
                 sync_mode=0,
                 serialize_from=-1,
                 debug=False,
                 debug_level=1,
                 cpu_device="/cpu:0"):
        """Create an LMS object to edit the graph for supporting large model.

        Args:
          graph: the graph we will modify for LMS. This should be the graph of
            user-defined neural network.
          excl_scopes: a set of scopes for operations whose tensors will not
            be swapped out to the host. Default `empty`.
          incl_scopes: a set of scopes for operations whose tensors will be
            swapped out to the host. Default `empty`.
          excl_types: a set of types for operations whose tensors will not be
            swapped out to the host. Default `empty`.
          incl_types: a set of types for operations whose tensors will be
            swapped out to the host. Default `empty`.
          swapout_threshold: if the topological-sort distance between the
            consuming operation and generating operation of a tensor is
            greater (>) than `swapout_threshold`, then trigger swapping the
            tensor. Default `-1` (auto mode).
          swapin_groupby: consuming operations whose distances among them are
            within `swapin_groupby` share the same swap-in operation.
            Default `0`.
          swapin_ahead: lower-bound value for LMS. A tensor will be swapped in
            during the backward phase at least `swapin_ahead` nodes before it
            in the graph. Default `-1` (auto mode).
          sync_mode: whether do synchronization between data transfer and
            kernel computation or not. Four modes: `0` turn off. `1` sync for
            only swap-out ops. `2` sync for only swap-in ops. `3` sync for both
            swap-out and swap-in ops. Default `0`.
          serialize_from: serialize operations at the same level in the
            topological sort. Levels starting from `serialize_from`
            to the end of the topological sort are serialized.
            Default `-1` (turn off).
          debug: debug mode for LMS. Default `False`.
          debug_level: debug level for LMS (1 or 2). Default `1`.
          cpu_device: the device we would like swap tensors to.
        """
        self._graph = graph
        self._excl_scopes = excl_scopes
        self._incl_scopes = incl_scopes
        self._excl_types = excl_types
        self._incl_types = incl_types

        self._swapout_threshold = swapout_threshold
        self._swapin_groupby = swapin_groupby
        self._swapin_ahead = swapin_ahead
        if sync_mode not in {0, 1, 2, 3}:
            raise ValueError('Invalid value for sync_mode')
        self._sync_mode = sync_mode
        self._serialize_from = serialize_from

        # https://github.com/tensorflow/tensorflow/blob/master/tensorflow/python/framework/graph_util_impl.py
        self._variable_ops = {
            "Assign",
            "AssignAdd",
            "AssignSub",
            "Queue",
            "ScatterAdd",
            "ScatterSub",
            "ScatterUpdate",
            "TruncatedNormal",
            "Variable",
            "VariableV2",
        }

        # variable ops: https://github.com/tensorflow/tensorflow/blob/master/tensorflow/compiler/tf2xla/kernels/variable_ops.cc
        self._unused_types = {
            # input data
            'Const', 'Identity', 'Read',
            'Placeholder', 'PlaceholderWithDefault',
            # variable ops
            'VarHandleOp', 'VarIsInitializedOp', 'VariableShape',
            'ReadVariableOp', 'AssignVariableOp',
            'AssignAddVariableOp', 'AssignSubVariableOp'
            'ResourceGather', 'ResourceScatterAdd',
            'ResourceScatterSub', 'ResourceScatterMul',
            'ResourceScatterDiv', 'ResourceScatterMin',
            'ResourceScatterMax', 'ResourceScatterUpdate',
            'ResourceScatterNdUpdate', 'ResourceScatterNdAdd',
            # data filling
            'Fill', 'Range', 'RandomUniform'}
        self._excl_types |= self._unused_types | self._variable_ops

        self._excl_ops = set()
        self._incl_ops = set()
        self._topo_sort = None
        self._cpu_device = cpu_device
        self._debug = debug
        self._debug_level = debug_level

        # keep the numbers of swap-out/swap-in ops
        self._swapout_ops = set()
        self._swapin_ops = set()

        # store information to be used to adding control dependencies
        self._ops_triples = []  # [(src_op, dest_op, swapin_op)]

        # control outputs topology
        self._control_outputs = None
        self._version = None

    def run(self, graph=None):
        """Edit the graph by adding swapin and swapout ops.

        Swapin and swapout ops are in the host.

        The graph is modified in-place.

        Return:

          a set of added ops.
        """
        if graph:
            self._graph = graph

        if not self._graph:
            raise ValueError('The dataflow graph is required but has not been'
                             ' provided.')
        self._version = self._graph.version

        self._log_info("Editing model for LMS")
        start_time = time.time()

        all_ops = self._graph.get_operations()
        for op in all_ops:
            if 'lms/swap' in op.name:
                self._log_info('This model has already been updated with LMS '
                               'swap operations. LMS will not re-process it.')
                return
        self._log_info(
            "The graph has {} ops in total".format(len(all_ops), 1))

        # exclusive ops
        self._excl_ops = self._filter_scopes_and_types(
            all_ops, self._excl_scopes, self._excl_types)
        # inclusive ops
        self._incl_ops = self._filter_scopes_and_types(
            all_ops, self._incl_scopes, self._incl_types)

        # build a topological sort
        self._topo_sort = topos.TOPOS(all_ops)
        self._topo_sort.build()
        if self._serialize_from >= 0:
            init_ops = self._force_variable_initialization(
                {op for op in all_ops
                 if op.type in {'Variable', 'VariableV2'}})
            if len(init_ops) > 0:
                # when are all variables initialized?
                self._topo_sort.reset()
                self._topo_sort.build()
                m = max({self._get_order(op) for op in init_ops})
                self._serialize_from = max(m+1, self._serialize_from)
            self._log_info("Serialize the topological sort from " +
                           "level {} to level {} (last level)".format(
                               self._serialize_from, self._topo_sort.size))
            self._topo_sort.serialize_from(self._serialize_from)

        self._log_info("Topological sort size: {}".format(
            self._topo_sort.size))
        for i in range(0, self._topo_sort.size):
            self._log_info("[{}]: {}".format(
                i, [(op.name, op.type)
                    for op in self._get_ops_by_order(i)]), 1)

        # get control outputs topology
        self._control_outputs = ut.build_control_outputs(self._graph)

        # roughly estimate swapin_threshold in auto mode
        if self._swapout_threshold < 0:
            self._log_info("Use auto mode for setting swapout_threshold")
            self._swapout_threshold = self._topo_sort.size//2

        self._print_configuration()
        self._do_action(all_ops)  # add swapout/swapin ops

        if self._sync_mode == 0:  # async mode
            self._add_control_dependencies()  # add ctrl. dependencies
        else:
            self._sync_ops(self._sync_mode)

        self._log_info(
            "Added {} operations to the model".format(
                len(self._swapout_ops) + len(self._swapin_ops)) +
            " ({} swap-out operations and {} swap-in operations)".format(
                len(self._swapout_ops), len(self._swapin_ops)))
        self._log_info("Editing model for LMS, took: {} ms".format(
            (time.time()-start_time)*1000))

    def _force_variable_initialization(self, vars):
        """
        For each variable, it should be assigned a value before
        the other ops consumming the variable.
        But, by analyzing a graph statically, we have no dependency
        bewteen a Assign operation and other read/write operations.
        So we need to create dependency explicitly.
        ```
        VariableV2:
          input: None
          output: reading ops, Assign, writing ops, ...
        ```
        """ 
        init_ops = set()
        for x in vars:
            outs = ut.fanouts(x)
            assign_ops = {op for op in outs if op.type == "Assign"}

            # initialization op is the first Assign op
            m = min({self._get_order(op) for op in assign_ops})
            first_assign_ops = {
                op for op in assign_ops
                if self._get_order(op) == m
            }
            if len(first_assign_ops) != 1:
                self._log_info("Variable: {}".format(x.name))
                self._log_info("Fanout ops: {}".format(
                    {(op.name, op.type, self._get_order(op))
                     for op in outs}))
                raise ValueError(
                    'Could not find the first assignment for {}'.format(x.name))

            # initialize the variable first
            for y in outs - first_assign_ops:
                self._add_control_inputs(y, first_assign_ops)
            init_ops |= first_assign_ops

        return init_ops

    def _do_action(self, src_ops):
        """Add swapin and swapout ops for ops that are reachable from `src_ops`.

        Args:
          src_ops: a list of `tf.Operation`
        """
        open_set = Queue.Queue()
        closed_set = set()

        for op in src_ops:
            open_set.put(op)

        while not open_set.empty():
            src_op = open_set.get()

            # get next ops before the graph is changed
            next_ops = ut.fanouts(src_op)

            # do action for src_op
            # bypass excluded ops
            if src_op in self._excl_ops:
                pass
            elif self._get_order(src_op) < 0:
                pass
            elif self._incl_ops:
                # if inclusive mode is enabled,
                # only proceed included ops
                if src_op in self._incl_ops:
                    self._insert_swap_nodes(src_op)
                else:
                    pass
            else:
                self._insert_swap_nodes(src_op)

            for op in next_ops:
                if op in closed_set:
                    continue
                if op not in open_set.queue:
                    open_set.put(op)

            closed_set.add(src_op)

    def _sync_ops(self, sync_mode):
        """TODO: write comment
        """
        # sync for swap-out ops
        if sync_mode in {1, 3}:
            # make sure we are working with the latest control outputs topology
            self._rebuild_control_outputs()
            src_ops = {op[0] for op in self._ops_triples}
            for x in src_ops:
                self._sync_swapout(x)

        # sync for swap-in ops
        if sync_mode in {2, 3}:
            dest_ops = {op[1] for op in self._ops_triples}
            for x in dest_ops:
                self._sync_swapin(x)

    def _sync_swapout(self, x):
        """TODO: write comment
        Need to update control outputs topology before calling
        this method
        """
        def _souts(op):
            return ut.fanouts(op) & self._swapout_ops

        x_souts = _souts(x)
        fs = ut.fanouts(x) | self._get_control_outputs(x)
        fs -= x_souts
        fs_cins = set()
        for op in fs:
            fs_cins |= set(op.control_inputs)

        cins = x_souts - fs_cins
        for op in fs:
            self._add_control_inputs(op, cins)
        self._update_control_outputs(cins, fs)

    def _sync_swapin(self, x):
        """TODO: write comment
        """
        def _sins(op):
            return ut.fanins(op) & self._swapin_ops

        x_sins = _sins(x)
        x_sins_outs = set()
        x_sins_cins = set()
        for op in x_sins:
            x_sins_cins |= set(op.control_inputs)
        fs = ut.fanins(x) | set(x.control_inputs)
        fs -= (x_sins | x_sins_outs | x_sins_cins)
        for op in x_sins:
            self._add_control_inputs(op, fs)
        
    def _groupby(self, ops, limit=5):
        """Group `ops` into groups so that topological distance between
        two consecutive ops in a group is within `limit`.

        Args:
          ops: a set of `tf.Operation`.
          limit: a threshold

        Return:
          A list of sets of `tf.Operation`.
        """
        ops_ords = [(op, self._get_order(op)) for op in ops]
        x = sorted([i[1] for i in ops_ords])
        xs = [(i, i) for i in x]

        ys = [xs[0]]
        for i in range(1, len(xs)):
            last = ys[-1]
            curr = xs[i]
            if (curr[0] - last[1] <= limit):
                last = (last[0], curr[1])
                ys[-1] = last
            else:
                ys.append(curr)

        zs = []
        for y in ys:
            gs = set()
            gs = {op[0]
                  for op in ops_ords
                  if (op[1] >= y[0] and op[1] <= y[1])}
            zs.append(gs)
        return zs

    def _insert_swap_nodes(self, src_op):
        """Insert swapin and swapout ops for the given operation into the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`
        """
        self._log_info("Operation: {}".format(src_op), 2)

        # filter candidates

        ts_dests = {}
        src_op_order = self._get_order(src_op)
        for ts in src_op.outputs:
            # filter by tensor shape
            # do not swap 1-dimension or unknown shape tensors.
            ndims = ts.shape.ndims
            if ndims is None or ndims <= 1:
                continue

            # filter by topological distance
            # candidates are ops whose distance to `src_op` is
            # greater than threshold
            cands = [
                op
                for op in ts.consumers()
                if self._get_order(op) - src_op_order > self._swapout_threshold
            ]
            if len(cands) == 0:
                continue
            else:
                ts_dests[ts] = cands

        if ts_dests:
            self._log_info("Operation: {}".format(src_op.name), 1)
        else:
            return

        for ts in ts_dests:
            # group near candidates by topological distance
            dests_grp = self._groupby(ts_dests[ts], self._swapin_groupby)

            # insert swapout and swap-in ops
            sout, dest_sin = self._insert_swap_nodes_for_ts(
                src_op, ts, dests_grp)

            # keep newly added ops
            self._swapout_ops.add(sout)
            self._excl_ops.add(sout)  # exclusive this op
            for dest, sin in dest_sin:
                self._swapin_ops.add(sin)
                self._excl_ops.add(sin)  # exclusive this op
                self._ops_triples.append((src_op, dest, sin))

    def _insert_swap_nodes_for_ts(self, src_op, ts, targets):
        """Insert swapin and swapout ops for the given tensor into the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          ts: a `tf.Tensor`, an output of `src_op`.
          targets: a list of sets of consuming ops of `src_op`.

        Return:
          A tuple of a swap-out op and a set of pairs of a consuming op and
          a swap-in op.
        """
        # create a swap_out node
        swapout_op = self._add_swapout(src_op, ts)
        self._add_control_inputs(swapout_op, src_op, offset=4)

        # create swap_in nodes
        dest_sin = set()
        for dest_ops in targets:
            # swap_in op
            swapin_op = self._add_swapin(swapout_op, dest_ops, ts)
            # for control dependency
            ops_ords = [(op, self._get_order(op)) for op in dest_ops]
            x = sorted([i[1] for i in ops_ords])[0]  # the earliest op
            dest_op = [op[0] for op in ops_ords if op[1] == x][0]
            dest_sin.add((dest_op, swapin_op))

        return (swapout_op, dest_sin)

    def _add_swapout(self, src_op, ts0):
        """Add a swapout operation to the graph to swap out the output tensor `ts0`
        of the operation `src_op`.

        This method does an in-place modification to the graph.

        Example: the graph before and after this method invoked.
        ```
        Before
          (src_op) -> |ts0| -> (dest_op)

        After:
          (src_op) -> |ts0| -> (swapout_op)
          |ts0| -> (dest_op)
        ```

        Args:
          src_op: a `tf.Operation` that produces the tensor `ts0`.
          ts0: a output `tf.Tensor` of `src_op` being swapped out.

        Return:
          A `tf.Operation` newly added to the graph.
        """
        with ops.device(self._cpu_device):
            swap_out = array_ops.identity(
                ts0,
                name="lms/swapout_{}".format(
                    ts0.name.replace("/", "_").replace(":", "_")))

        # Connect: src-node -> swap-out
        ut.reroute_input(ts0, swap_out.op.inputs[0], swap_out.op)
        self._log_info("Swap-out: Tensor {} (shape: {})".format(
            ts0.name, ts0.shape), 1, 2)
        self._log_info("Swap-out operation: {}".format(
                swap_out.op.name), 1, 4)
        self._log_info("Connect: {} => {}".format(
            src_op.name, swap_out.op.name), 1, 4)

        return swap_out.op

    def _add_swapin(self, swapout_op, dest_ops, ts0):
        """Add a swapin operation to the graph. The swapin ops reads
        the output tensor of `swapout_op` and passes it to `dest_ops`,
        replacing the input tensors `ts0` of `dest_ops`.

        This method does an in-place modification to the graph.

        Example: the graph before and after this method invoked.
        ```
        Before
          |ts0| -> (swapout_op)
          |ts0| -> (dest_op)

        After:
          |ts0| -> (swapout_op) -> (swapin_op) -> (dest_op)
        ```

        Args:
          swapout_op: a `tf.Operation` that swapped out the tensor `ts0`.
          dest_ops: a set of `tf.Operation` that will consume the output 
                    tensor of `swapout_op`.
          ts0: a `tf.Tensor` being the original input tensor of `dest_op`.

        Return:
          A `tf.Operation` newly added to the graph.
        """
        self._log_info("Swap-in: Tensor {} (shape: {})".format(
            ts0.name, ts0.shape), 1, 2)

        with ops.device(self._cpu_device):
            swap_in = array_ops.identity(
                ts0,
                name="lms/swapin_{}".format(
                    ts0.name.replace("/", "_").replace(":", "_")))

        # Connect: swap_out -> swap_in
        ut.reroute_input(swapout_op.outputs[0],
                         swap_in.op.inputs[0], swap_in.op)
        self._log_info("Swap-in operation: {}".format(
            swap_in.op.name), 1, 4)
        self._log_info("Connect: {} => {}".format(
            swapout_op.name, swap_in.op.name), 1, 4)

        # Connect: swap_in -> dest_ops
        for dest_op in dest_ops:
            ut.reroute_input(swap_in.op.outputs[0], ts0, dest_op)
            self._log_info("Connect: {} => {}".format(
                swap_in.op.name, dest_op.name), 1, 4)
        return swap_in.op

    def _add_control_dependencies(self):
        """Add control dependency operations for all consuming ops.
        """
        if (self._swapin_ahead < 0):
            self._sequential_strategy()
        else:
            # Use the user-defined ahead
            for op in self._ops_triples:
                self._add_control_dependency(
                    op[0], op[1], op[2], self._swapin_ahead)

    def _sequential_strategy(self):
        """This strategy is to make sure swapins are done in
        a sequential way with respect to the topological order of
        consuming ops.
        """
        x = sorted(self._ops_triples,
                   key=lambda ops: self._get_order(ops[1]))

        # a fixed setting for the first swapins.
        x0_dest_order = self._get_order(x[0][1])
        ahead = 3
        k = 0
        for i in range(0, len(x)):
            if self._get_order(x[i][1]) == x0_dest_order:
                self._add_control_dependency(x[i][0], x[i][1], x[i][2], ahead)
                k = i
            else:
                break

        lb = x0_dest_order
        last_order = lb
        for i in range(k+1, len(x)):
            curr_order = self._get_order(x[i][1])
            if curr_order != last_order:
                lb = last_order
                last_order = curr_order
            ahead = curr_order - lb
            self._add_control_dependency(x[i][0], x[i][1], x[i][2], ahead)

    def _add_control_dependency(self, src_op, dest_op, swapin_op, ahead):
        """Find and add a control dependency to the graph.

        This method does an in-place modification to the graph.

        Args:
          src_op: a `tf.Operation`.
          dest_op: a `tf.Operation`.
          swapin_op: a `tf.Operation`.
        """
        re = self._do_direct_order(src_op, dest_op, ahead)

        ctrld_op = re[0]
        if ctrld_op:
            self._add_control_inputs(swapin_op, ctrld_op)
        else:
            self._log_info(
                "No control dependency op found for the swap-in {}.".format(
                    swapin_op.name), 1)
            self._log_info(
                "Do synchronization for the swap-in {}.".format(
                    swapin_op.name), 1, 2)
            self._sync_swapin(dest_op)

    def _do_direct_order(self, fw_op, src_op, distance):
        """Find a control dependency operation using topological sort.

        Args:
          fw_op: a `tf.Operation` that has a tensor swapped out.
          bw_op: a `tf.Operation` that consumes a tensor swapped in.
          distance: an `integer`. The distance in the topological order
            between `bw_op` and a candidate for control dependency ops
            must be greater than `distance`.

        Return:
          A tuple of (`tf.Operation`, an `integer`). The first item is
          the control dependency operation that triggers swapping in the input
          tensor of `bw_op`. The second item is the order of the control
          dependency operation in the topological order.
        """
        result_ops = set()

        # offset ordering
        fw_order = self._get_order(fw_op)
        src_order = self._get_order(src_op)

        range_ub = src_order - distance
        range_lb = fw_order + 1

        ctrld_order = -1
        for i in reversed(range(range_lb, range_ub)):
            candidates = self._get_ops_by_order(i)
            # on the chain rule path
            candidates = {op
                          for op in candidates
                          if self._is_reachable(op, src_op)}
            candidates = {op
                          for op in candidates
                          if "/cond/" not in op.name}
            if candidates:
                result_ops |= candidates
                ctrld_order = i
                break

        if result_ops:
            ctrld_op = next(iter(result_ops))
            return (ctrld_op, ctrld_order)
        else:
            return (None, -1)

    def _filter_scopes_and_types(self, within_ops, scopes, types):
        """Filter out ops that are not in `scopes` and not of `types`.

        Args:
          within_ops: an object convertible to a list of `tf.Operation`.
          scopes: a list of scope path.
          types: a list of tf.DataType.
        Return:
          A set of `tf.Operation`.
        """
        ops = set()
        for scope in scopes:
            ops |= {op for op in within_ops
                    if scope in op.name}
        ops |= {op
                for op in within_ops
                if op.type in types}
        return ops

    def _is_reachable(self, src_op, dest_op):
        """Check whether there exists a path from src_op to dest_op
        Args:
          src_op: a starting operation.
          dest_op: a destination operation.

        Return:
          True/False.
        """
        ret = False
        dest_ord = self._get_order(dest_op)

        open_set = Queue.Queue()
        open_set.put(src_op)

        closed_set = set()
        while not open_set.empty():
            src_op = open_set.get()
            next_ops = ut.fanouts(src_op)
            next_ops |= self._get_control_outputs(src_op)
            
            next_ops = {op for op in next_ops
                        if self._get_order(op) <= dest_ord}
            if dest_op in next_ops:
                return True
            else:
                for nop in next_ops:
                    if nop in closed_set:
                        continue
                    else:
                        open_set.put(nop)
        return ret

    def _get_order(self, op):
        """Return the topological order of an operation.

        Args:
          op: a `tf.Operation`.

        Return:
          an integer.
        """
        ret = self._topo_sort.get_order(op)
        if ret is None:
            return -1
        else:
            return ret

    def _get_ops_by_order(self, order):
        """Return a set of ops with the given order.
        
        Args:
          order: an integer.

        Return:
          a set of `tf.Operation`
        """
        return self._topo_sort.get_ops(order)

    def _log_info(self, message, level=0, offset=1):
        """Log debug information.

        Args:
          message: a formatted string.
          level: an `integer`.
        """
        if level == 0 or (self._debug and self._debug_level >= level):
            # Use tf_logging.info instead of print, since print
            # is not thread safe, which can break tests.
            tf_logging.info("[LMS][{}]".format(level) +
                            ' '*offset +
                            "{}".format(message))

    def _print_configuration(self):
        """Print configuration information about LMS.
        """
        self._log_info("swapout_threshold: {}".format(self._swapout_threshold))
        self._log_info("swapin_groupby: {}".format(self._swapin_groupby))
        if self._sync_mode == 1:
            self._log_info(
                "sync_mode was turned on for swap-out ops")
            self._log_info("swapin_ahead: {}".format(
                "auto mode" if self._swapin_ahead < 0 else self._swapin_ahead))
        elif self._sync_mode == 2:
            self._log_info(
                "sync_mode was turned on for swap-in ops. " +
                "swapin_ahead will be ignored")
        elif self._sync_mode >= 3:
            self._log_info(
                "sync_mode was turned on for both swap-out and swap-in ops. " +
                "swapin_ahead will be ignored")
        elif self._sync_mode == 0:
            self._log_info("swapin_ahead: {}".format(
                "auto mode" if self._swapin_ahead < 0 else self._swapin_ahead))
        else:
            pass

    def _get_control_outputs(self, op):
        """Return a set of control outputs of an operation.
        """
        if op in self._control_outputs:
            return self._control_outputs[op]
        else:
            return set()

    def _rebuild_control_outputs(self):
        """Rebuild the control_outputs dictionary if there are ops added
        to the graph.
        """
        if self._version != self._graph.version:
            self._control_outputs = ut.build_control_outputs(self._graph)

    def _update_control_outputs(self, ops, couts):
        """Update control_output sets for operations in `ops`.
        """
        for op in ops:
            if op in self._control_outputs:
                self._control_outputs[op] |= couts
            else:
                self._control_outputs[op] = couts
    
    def _add_control_inputs(self, op, cops, offset=0):
        """Add control dependencies from `cops` to `op`.

        Args:
          op: a tf.Operation to which the control inputs are added.
          cops: an object convertible to a list of `tf.Operation`.
        Raises:
          TypeError: if op is not a tf.Operation
          ValueError: if any cop in cops is already a control input of op.
        """
        ut.add_control_inputs(op, cops)
        flag = True
        try:
            _ = iter(cops)
        except Exception:  # pylint: disable=broad-except
            flag = False

        if not flag:
            self._log_info(
                "Control dependency: {} => {}".format(
                    cops.name, op.name), 1, offset)
        else:
            for cop in cops:
                self._log_info(
                    "Control dependency: {} => {}".format(
                        cop.name, op.name), 1, offset)
