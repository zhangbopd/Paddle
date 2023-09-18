#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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
"""Definition of PSLib."""

import os
import sys
from .optimizer_factory import FLEET_GLOBAL_DICT  # noqa: F403
from .optimizer_factory import DistributedAdam  # noqa: F403
from google.protobuf import text_format
from paddle.framework import core
from paddle.incubate.distributed.fleet.base import Fleet
from paddle.incubate.distributed.fleet.base import Mode
from paddle.incubate.distributed.fleet.base import DistributedOptimizer
from paddle.incubate.distributed.fleet.role_maker import MPISymetricRoleMaker
from paddle.incubate.distributed.fleet.role_maker import HeterRoleMaker
from paddle.common_ops_import import LayerHelper

import paddle


class PSLib(Fleet):
    """PSLib class."""

    def __init__(self):
        super().__init__(Mode.PSLIB)
        self._opt_info = None
        self._local_ip = 0
        self._fleet_ptr = None
        self._main_programs = []
        self._scopes = []
        self._client2client_request_timeout_ms = 500000
        self._client2client_connect_timeout_ms = 10000
        self._client2client_max_retry = 3

    def init(self, role_maker=None):
        if role_maker is None:
            role_maker = MPISymetricRoleMaker()
        super().init(role_maker)
        self._fleet_ptr = core.Fleet()
        self._heter_ptr = None
        if isinstance(role_maker, HeterRoleMaker):
            self._heter_ptr = core.Heter()

    def _set_client_communication_config(
        self, request_timeout_ms, connect_timeout_ms, max_retry
    ):
        self._client2client_request_timeout_ms = request_timeout_ms
        self._client2client_connect_timeout_ms = connect_timeout_ms
        self._client2client_max_retry = max_retry

    def set_pull_local_thread_num(self, thread_num):
        self._fleet_ptr.set_pull_local_thread_num(thread_num)

    def init_worker(self):
        """
        init_worker(): will be called by user. When a user knows current process is_server(), he/she
                    should call init_worker() to initialize global information about worker and connect
                    worker with pserver. You should run startup program before init_worker.
        Args:
            executor (Executor): The executor to run for init server.
            programs (Program|None): The program that need to run.
        """

        if len(self._main_programs) == 0:
            raise ValueError(
                "You should run DistributedOptimizer.minimize() first"
            )

        if self._opt_info:
            if "fleet_desc" in self._opt_info:
                self._dist_desc_str = text_format.MessageToString(
                    self._opt_info["fleet_desc"]
                )
                self._dist_desc = self._opt_info["fleet_desc"]
            else:
                raise Exception(
                    "You should run DistributedOptimizer.minimize() first"
                )
            # barrier_all for init_server, wait for server starts
            if isinstance(self._role_maker, HeterRoleMaker):
                if self._role_maker.is_xpu():
                    local_endpoint = self._role_maker.get_local_endpoint()
                    local_endpoint = local_endpoint.split(":")
                    self._heter_ptr.start_xpu_service(
                        str(local_endpoint[0]), int(local_endpoint[1])
                    )
            self._role_maker._barrier_all()
            self.all_ips_ = self._role_maker._all_gather(self._local_ip)
            # worker_index * 2 is for compatible with older versions of pslib
            self._fleet_ptr.init_worker(
                self._dist_desc_str,
                self.all_ips_,
                self._role_maker._get_size(),
                self._role_maker.worker_index() * 2,
            )
            if isinstance(self._role_maker, HeterRoleMaker):
                if self._role_maker.is_worker():
                    self._heter_ptr.set_xpu_list(
                        self._role_maker._xpu_endpoints
                    )
                    self._heter_ptr.create_client2xpu_connection()
            # barrier_all for init_worker
            self._role_maker._barrier_all()
            # prepare for client to client communication
            if self._role_maker.is_worker():
                info = self._fleet_ptr.get_clients_info()
                print(f"Client Info: {info}")
                all_info = self._role_maker._worker_gather(info[0])
                print(f"All Client Info: {all_info}")
                self._fleet_ptr.gather_clients(all_info)
                self._fleet_ptr.set_client2client_config(
                    self._client2client_request_timeout_ms,
                    self._client2client_connect_timeout_ms,
                    self._client2client_max_retry,
                )
                self._fleet_ptr.create_client2client_connection()
            # barrier for init model
            self._role_maker._barrier_worker()
            if self._role_maker.is_first_worker():
                tables = []
                for tp in self._dist_desc.trainer_param:
                    for i in tp.dense_table:
                        tables.append(i)
                for prog, scope in zip(self._main_programs, self._scopes):
                    prog_id = str(id(prog))
                    prog_conf = self._opt_info['program_configs'][prog_id]
                    prog_tables = {}
                    for key in prog_conf:
                        if "dense" not in key:
                            continue
                        for table_id in prog_conf[key]:
                            prog_tables[int(table_id)] = 0
                    for table in tables:
                        if int(table.table_id) not in prog_tables:
                            continue
                        var_name_list = []
                        for i in range(0, len(table.dense_variable_name)):
                            var_name = table.dense_variable_name[i]
                            if scope.find_var(var_name) is None:
                                raise ValueError(
                                    "var "
                                    + var_name
                                    + " not found in scope, "
                                    + "you should run startup program first"
                                )
                            var_name_list.append(var_name)
                        if not self._opt_info["use_ps_gpu"]:
                            self._fleet_ptr.init_model(
                                scope, int(table.table_id), var_name_list
                            )
            # barrier for init model done
            self._role_maker._barrier_worker()
        else:
            raise NameError(
                "You should run DistributedOptimizer.minimize() first"
            )

    def init_server(self, model_dir=None, **kwargs):
        """
        Called by user. It will load model from model_dir.

        Args:
            model_dir(str, optional): Load model path, can be local or hdfs/afs path. Default is None.
            kwargs: User-defined attributes, currently support following:

                - model(int): Load model mode.

                  0 is for load whole model,
                  1 is for load delta model (load diff).
                  Default is 0.

        Examples:

            .. code-block:: text

                fleet.init_server("/you/path/to/model", mode = 0)

        """
        mode = kwargs.get("mode", 0)
        if isinstance(self._role_maker, HeterRoleMaker):
            self._role_maker._barrier_xpu()
            if self._role_maker.is_first_xpu():
                self._fleet_ptr.load_model(model_dir, mode)
            self._role_maker._barrier_xpu()
        else:
            self._role_maker._barrier_worker()
            if self._role_maker.is_first_worker():
                self._fleet_ptr.load_model(model_dir, mode)
            self._role_maker._barrier_worker()

    def run_server(self):
        """
        Called by user. When a user init server, after that he/she should run run_server() to start.
        """
        if self._opt_info:
            if "fleet_desc" in self._opt_info:
                self._dist_desc_str = text_format.MessageToString(
                    self._opt_info["fleet_desc"]
                )
                self._dist_desc = self._opt_info["fleet_desc"]
            else:
                raise Exception(
                    "You should run DistributedOptimizer.minimize() first"
                )
            # server_index * 2 is for compatible with older versions of pslib
            self._fleet_ptr.init_server(
                self._dist_desc_str, self._role_maker.server_index() * 2
            )
            if isinstance(self._role_maker, MPISymetricRoleMaker):
                self._local_ip = self._fleet_ptr.run_server()
            else:
                local_endpoint = self._role_maker.get_local_endpoint()
                local_endpoint = local_endpoint.split(":")
                self._local_ip = self._fleet_ptr.run_server(
                    str(local_endpoint[0]), int(local_endpoint[1])
                )

            # barrier_all for init_server
            self._role_maker._barrier_all()
            self.all_ips_ = self._role_maker._all_gather(self._local_ip)

            self._fleet_ptr.gather_servers(
                self.all_ips_, self._role_maker._get_size()
            )
            # barrier_all for init_worker, wait all workers start
            self._role_maker._barrier_all()
        else:
            raise Exception(
                "You should run DistributedOptimizer.minimize() first"
            )

    def end_pass(self, scope):
        if self._role_maker.worker_index() < self._role_maker.xpu_num():
            self._heter_ptr.end_pass(scope, self._role_maker.worker_index())
            self._heter_ptr.stop_xpu_service(self._role_maker.worker_index())

    def train_from_dataset(
        self,
        executor,
        program=None,
        dataset=None,
        scope=None,
        thread=0,
        debug=False,
        fetch_list=None,
        fetch_info=None,
        print_period=100,
        fetch_handler=None,
    ):
        """ """

        if self._role_maker.is_worker():
            self._role_maker._barrier_heter()
        executor.train_from_dataset(
            program,
            dataset,
            scope,
            thread,
            debug,
            fetch_list,
            fetch_info,
            print_period,
            fetch_handler,
        )

    def start_heter_trainer(
        self,
        executor,
        program=None,
        scope=None,
        debug=False,
        fetch_list=None,
        fetch_info=None,
        print_period=100,
        fetch_handler=None,
    ):
        """ """

        trainer_instance = executor.start_heter_trainer(
            program,
            scope,
            debug,
            fetch_list,
            fetch_info,
            print_period,
            fetch_handler,
        )
        if self._role_maker.is_xpu():
            print("barrier heter")
            self._role_maker._barrier_heter()
            print("barrier heter")
        executor._default_executor.release_trainer(trainer_instance)

    def stop_worker(self):
        """
        Will be called after a user finishes his/her training task. Fleet instance will be
            destroyed when stop_worker() is called.
        """
        self._role_maker._barrier_worker()
        # all worker should be finalize first
        if self._role_maker.is_worker():
            self._fleet_ptr.finalize_worker()
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.stop_server()
        if self._heter_ptr:
            self._heter_ptr.stop_xpu_service()
        self._role_maker._barrier_worker()
        self._role_maker._barrier_all()
        self._role_maker._finalize()

    def distributed_optimizer(self, optimizer, strategy={}):
        """
        distributed_optimizer

        Args:
            optimizer (Optimizer): Optimizer.
            strategy (dict): Strategy.

        Returns:
            optimizer(DownpourOptimizer): Downpour optimizer.

        Examples:

            .. code-block:: text

                fleet.distributed_optimizer(optimizer)

        """
        self._optimizer = DownpourOptimizer(optimizer, strategy)
        return self._optimizer

    def save_inference_model(
        self,
        executor,
        dirname,
        feeded_var_names=None,
        target_vars=None,
        main_program=None,
        export_for_deployment=True,
    ):
        """
        Save pserver model called from a worker.

        Args:

            executor (Executor): Fluid executor.
            dirname (str): Save model path.
            feeded_var_names (list, optional): Default None.
            target_vars (list, optional): Default None.
            main_program (Program, optional): Default None.
            export_for_deployment (bool, optional): Default None.

        Examples:

            .. code-block:: text

                fleet.save_inference_model(dirname="hdfs:/my/path")

        """
        self._fleet_ptr.save_model(dirname, 0)

    def print_table_stat(self, table_id, pass_id, threshold):
        """
        Print stat info of table_id, format: tableid, feasign size, mf size.

        Args:

            table_id (int): The id of table.
            pass_id (int): The id of pass.
            threshold (float): The threshold of print.

        Examples:

            .. code-block:: text

                fleet.print_table_stat(0)

        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.print_table_stat(table_id, pass_id, threshold)
        self._role_maker._barrier_worker()

    def set_file_num_one_shard(self, table_id, file_num):
        """
        Set file_num in one shard.

        Args:

            table_id (int): The id of table.
            file_num (int): File num in one shard.

        Examples:

            .. code-block:: text

                fleet.set_file_num_one_shard(0, 5)

        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.set_file_num_one_shard(table_id, file_num)
        self._role_maker._barrier_worker()

    def save_persistables(self, executor, dirname, main_program=None, **kwargs):
        """
        Save presistable parameters,
        when using fleet, it will save sparse and dense feature.

        Args:

            executor (Executor): Fluid executor.
            dirname (str): Save path. It can be hdfs/afs path or local path.
            main_program (Program, optional): Fluid program, default None.
            kwargs: Use define property, current support following

                - mode (int):
                  0 means save all pserver model,
                  1 means save delta pserver model (save diff),
                  2 means save xbox base,
                  3 means save batch model.

        Examples:

            .. code-block:: test

                fleet.save_persistables(dirname="/you/path/to/model", mode = 0)

        """
        mode = kwargs.get("mode", 0)
        self._fleet_ptr.client_flush()
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.save_model(dirname, mode)
        self._role_maker._barrier_worker()

    def save_model_with_whitelist(
        self, executor, dirname, whitelist_path, main_program=None, **kwargs
    ):
        """
        Save whitelist, mode is consistent with fleet.save_persistables,
        when using fleet, it will save sparse and dense feature.

        Args:

            executor (Executor): Fluid executor.
            dirname (str): save path. It can be hdfs/afs path or local path.
            whitelist_path (str): whitelist path. It can be hdfs/afs path or local path.
            main_program (Program, optional): fluid program, default None.
            kwargs: Use define property, current support following

                - mode (int):
                  0 means save all pserver model,
                  1 means save delta pserver model (save diff),
                  2 means save xbox base,
                  3 means save batch model.

        Examples:

            .. code-block:: text

                fleet.save_persistables(dirname="/you/path/to/model", mode = 0)

        """
        mode = kwargs.get("mode", 0)
        table_id = kwargs.get("table_id", 0)
        self._fleet_ptr.client_flush()
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.save_model_with_whitelist(
                table_id, dirname, mode, whitelist_path
            )
        self._role_maker._barrier_worker()

    def save_multi_table_one_path(self, table_ids, model_dir, **kwargs):
        """
        Save pslib multi sparse table in one path.

        Args:

            table_ids (list): Table ids.
            model_dir (str): If you use hdfs, model_dir should starts with 'hdfs:', otherwise means local dir.
            kwargs (dict): User-defined properties.

                - mode (int): The modes illustrated above, default 0.
                - prefix (str): the parts to save can have prefix, for example, part-prefix-000-00000.

        Examples:

            .. code-block:: text

                fleet.save_multi_table_one_path("[0, 1]", "afs:/user/path/")

        """
        mode = kwargs.get("mode", 0)
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.save_multi_table_one_path(
                table_ids, model_dir, mode
            )
        self._role_maker._barrier_worker()

    def save_cache_model(self, executor, dirname, main_program=None, **kwargs):
        """
        Save sparse cache table,
        when using fleet, it will save sparse cache table.

        Args:

            executor (Executor): Fluid executor.
            dirname (str): Save path. It can be hdfs/afs path or local path.
            main_program (Program, optional): Fluid program, default None.
            kwargs: Use define property, current support following

                - mode (int): Define for feature extension in the future,
                  currently no use, will pass a default value 0.
                - table_id (int): Which table to save cache, default is 0.

        Returns:

            feasign_num (int): cache feasign num.

        Examples:

            .. code-block:: text

                fleet.save_cache_model(None, dirname="/you/path/to/model", mode = 0)

        """
        mode = kwargs.get("mode", 0)
        table_id = kwargs.get("table_id", 0)
        self._fleet_ptr.client_flush()
        self._role_maker._barrier_worker()
        cache_threshold = 0.0

        if self._role_maker.is_first_worker():
            cache_threshold = self._fleet_ptr.get_cache_threshold(table_id)
        # check cache threshold right or not
        self._role_maker._barrier_worker()

        if self._role_maker.is_first_worker():
            self._fleet_ptr.cache_shuffle(
                table_id, dirname, mode, cache_threshold
            )

        self._role_maker._barrier_worker()

        feasign_num = -1
        if self._role_maker.is_first_worker():
            feasign_num = self._fleet_ptr.save_cache(table_id, dirname, mode)

        self._role_maker._barrier_worker()
        return feasign_num

    def shrink_sparse_table(self):
        """
        Shrink cvm of all sparse embedding in pserver, the decay rate
        is defined as "show_click_decay_rate" in fleet_desc.prototxt.

        Examples:

            .. code-block:: text

                fleet.shrink_sparse_table()

        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            tables = []
            for tp in self._opt_info["fleet_desc"].trainer_param:
                for i in tp.sparse_table:
                    tables.append(i.table_id)
            for i in list(set(tables)):
                self._fleet_ptr.shrink_sparse_table(i)
        self._role_maker._barrier_worker()

    def shrink_dense_table(self, decay, emb_dim=11, scope=None, table_id=None):
        """
        Shrink batch_sum in pserver by multiplying by decay.

        Args:
            decay (float): The decay rate, usually range in (0, 1).
            emb_dim (int, optional): One element's length in datanorm layer. Default is 11.
            scope (Scope, optional): Scope object, default is base.global_scope(). Default is None.
            table_id (int, optional): Table id of shrinking dense table. None means shrink all,
                you should specify it when using multiple scopes, default is None.

        Examples:

            .. code-block:: text

                fleet.shrink_dense_table(0.98, 11, myscope1, 1)
                fleet.shrink_dense_table(0.98, 11, myscope1, 2)
                fleet.shrink_dense_table(0.98, 11, myscope2, 3)
        """
        if scope is None:
            scope = paddle.static.global_scope()
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            for tp in self._opt_info["fleet_desc"].trainer_param:
                for i in tp.dense_table:
                    if table_id is not None and table_id != i.table_id:
                        continue
                    var_list = list(i.dense_variable_name)
                    skip = False
                    for var in var_list:
                        if scope.find_var(var) is None:
                            skip = True
                            break
                    if skip:
                        continue
                    self._fleet_ptr.shrink_dense_table(
                        i.table_id, scope, var_list, decay, emb_dim
                    )
        self._role_maker._barrier_worker()

    def clear_one_table(self, table_id):
        """
        This function will be called by user. It will clear one table.

        Args:

            table_id (int): Table id.

        Examples:

            .. code-block:: text

                fleet.clear_one_table(0)
        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.clear_one_table(table_id)
        self._role_maker._barrier_worker()

    def clear_model(self):
        """
        This function will be called by user. It will clear sparse model.

        Examples:

            .. code-block:: text

                fleet.clear_model()
        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.clear_model()
        self._role_maker._barrier_worker()

    def load_pslib_whitelist(self, table_id, model_path, **kwargs):
        """
        Load pslib model for one table with whitelist.

        Args:
            table_id (int): Load table id.
            model_path (str): Load model path, can be local or hdfs/afs path.
            kwargs (dict): User defined params, currently support following:

                - only for load pslib model for one table:
                  mode (int): load model mode. 0 is for load whole model, 1 is for load delta model (load diff), default is 0.
                - only for load params from paddle model:
                  scope (Scope): Scope object.
                  model_proto_file (str): Path of program desc proto binary file, can be local or hdfs/afs file.
                  var_names (list): Var name list.
                  load_combine (bool): Load from a file or split param files, default False.

        Examples:

            .. code-block:: text

                # load pslib model for one table
                fleet.load_one_table(0, "hdfs:/my_fleet_model/20190714/0/")
                fleet.load_one_table(1, "hdfs:/xx/xxx", mode = 0)

                # load params from paddle model
                fleet.load_one_table(2, "hdfs:/my_paddle_model/",
                                    scope = my_scope,
                                    model_proto_file = "./my_program.bin",
                                    load_combine = False)

                # below is how to save proto binary file
                with open("my_program.bin", "wb") as fout:
                    my_program = base.default_main_program()
                    fout.write(my_program.desc.serialize_to_string())

        """
        self._role_maker._barrier_worker()
        mode = kwargs.get("mode", 0)
        if self._role_maker.is_first_worker():
            self._fleet_ptr.load_table_with_whitelist(
                table_id, model_path, mode
            )
        self._role_maker._barrier_worker()

    def load_one_table(self, table_id, model_path, **kwargs):
        """
        Load pslib model for one table or load params from paddle model.

        Args:

            table_id (int): Load table id.
            model_path (str): Load model path, can be local or hdfs/afs path.
            kwargs (dict): user defined params, currently support following:

                - only for load pslib model for one table:
                  mode(int): load model mode. 0 is for load whole model, 1 is for load delta model (load diff), default is 0.
                - only for load params from paddle model:
                  scope(Scope): Scope object.
                  model_proto_file(str): Path of program desc proto binary file, can be local or hdfs/afs file.
                  var_names(list): var name list.
                  load_combine(bool): load from a file or split param files, default False.

        Examples:

            .. code-block:: text

                # load pslib model for one table
                fleet.load_one_table(0, "hdfs:/my_fleet_model/20190714/0/")
                fleet.load_one_table(1, "hdfs:/xx/xxx", mode = 0)
                # load params from paddle model
                fleet.load_one_table(2, "hdfs:/my_paddle_model/",
                                    scope = my_scope,
                                    model_proto_file = "./my_program.bin",
                                    load_combine = False)
                # below is how to save proto binary file
                with open("my_program.bin", "wb") as fout:
                    my_program = base.default_main_program()
                    fout.write(my_program.desc.serialize_to_string())

        """
        self._role_maker._barrier_worker()
        mode = kwargs.get("mode", 0)
        scope = kwargs.get("scope", None)
        model_proto_file = kwargs.get("model_proto_file", None)
        var_names = kwargs.get("var_names", None)
        load_combine = kwargs.get("load_combine", False)
        self._role_maker._barrier_worker()
        if scope is not None and model_proto_file is not None:
            self._load_one_table_from_paddle_model(
                scope,
                table_id,
                model_path,
                model_proto_file,
                var_names,
                load_combine,
            )
        elif self._role_maker.is_first_worker():
            self._fleet_ptr.load_model_one_table(table_id, model_path, mode)
        self._role_maker._barrier_worker()

    def _load_one_table_from_paddle_model(
        self,
        scope,
        table_id,
        model_path,
        model_proto_file,
        var_names=None,
        load_combine=False,
    ):
        """
        Load params from paddle model, and push params to pserver.

        Args:
            scope (Scope): Scope object.
            table_id (int): The id of table to load.
            model_path (str): Path of paddle model, can be local or hdfs/afs file.
            model_proto_file (str): Path of program desc proto binary file, can be local or hdfs/afs file.
            var_names (list, optional): Load var names. Default is None.
            load_combine (bool, optional): Load from a file or split param files. Default is False.

        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            # get fs config from fleet_desc
            fs_name = self._opt_info["fleet_desc"].fs_client_param.uri
            fs_ugi = (
                self._opt_info["fleet_desc"].fs_client_param.user
                + ","
                + self._opt_info["fleet_desc"].fs_client_param.passwd
            )
            hadoop_bin = self._opt_info["fleet_desc"].fs_client_param.hadoop_bin
            # download model_path if it's hdfs/afs
            if model_path.startswith("hdfs:") or model_path.startswith("afs:"):
                dest = "./model_for_load_table_%s" % table_id
                cmd = (
                    hadoop_bin
                    + " fs -D fs.default.name="
                    + fs_name
                    + " -D hadoop.job.ugi="
                    + fs_ugi
                    + " -get "
                    + model_path
                    + " "
                    + dest
                )
                ret = os.system(cmd)
                if ret != 0:
                    raise RuntimeError("download model failed")
                model_path = dest
            # download model_proto_file if it's hdfs/afs
            if model_proto_file.startswith(
                "hdfs:"
            ) or model_proto_file.startswith("afs:"):
                dest = "./model_proto_file_for_load_table_%s" % table_id
                cmd = (
                    hadoop_bin
                    + " fs -D fs.default.name="
                    + fs_name
                    + " -D hadoop.job.ugi="
                    + fs_ugi
                    + " -get "
                    + model_proto_file
                    + " "
                    + dest
                )
                ret = os.system(cmd)
                if ret != 0:
                    raise RuntimeError("download model proto file failed")
                model_proto_file = dest
            for tp in self._opt_info["fleet_desc"].trainer_param:
                for i in tp.dense_table:
                    if table_id is not None and table_id != i.table_id:
                        continue
                    table_var_names = list(i.dense_variable_name)
                    skip = False
                    for var in table_var_names:
                        if scope.find_var(var) is None:
                            skip = True
                            break
                    if skip:
                        continue
                    self._fleet_ptr.load_from_paddle_model(
                        scope,
                        table_id,
                        var_names,
                        model_path,
                        model_proto_file,
                        table_var_names,
                        load_combine,
                    )
        self._role_maker._barrier_worker()

    def confirm(self):
        """
        confirm all the updated params in current pass
        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.confirm()
        self._role_maker._barrier_worker()

    def revert(self):
        """
        revert all the updated params in current pass
        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.revert()
        self._role_maker._barrier_worker()

    def load_model(self, model_dir=None, **kwargs):
        """
        load pslib model, there are at least 4 modes, these modes are the same
        in load one table/save model/save one table:
        0: load checkpoint model
        1: load delta model (delta means diff, it's usually for online predict)
        2: load base model (base model filters some feasigns in checkpoint, it's
           usually for online predict)
        3: load batch model (do some statistic works in checkpoint, such as
           calculate unseen days of each feasign)

        Args:
            model_dir (str, optional): If you use hdfs, model_dir should starts with
                'hdfs:', otherwise means local dir. Default is None.
            kwargs (dict): user-defined properties.

                - mode (int): The modes illustrated above, default 0.

        Examples:
            .. code-block:: text

                fleet.load_model("afs:/user/path/")
        """
        mode = kwargs.get("mode", 0)
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.load_model(model_dir, mode)
        self._role_maker._barrier_worker()

    def save_model(self, model_dir=None, **kwargs):
        """
        save pslib model, the modes are same with load model.

        Args:
            model_dir (str, optional): If you use hdfs, model_dir should starts with
                'hdfs:', otherwise means local dir. Default is None.
            kwargs (dict): user-defined properties.

                - mode (int): The modes illustrated above, default 0.

        Examples:
            .. code-block:: text

                fleet.save_model("afs:/user/path/")

        """
        mode = kwargs.get("mode", 0)
        prefix = kwargs.get("prefix", None)
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.save_model(model_dir, mode)
        self._role_maker._barrier_worker()

    def save_one_table(self, table_id, model_dir, **kwargs):
        """
        Save pslib model's one table, the modes are same with load model.

        Args:
            table_id (int): Table id.
            model_dir (str): if you use hdfs, model_dir should starts with
                'hdfs:', otherwise means local dir.
            kwargs (dict): user-defined properties.

                - mode (int): the modes illustrated above, default 0.
                - prefix (str): the parts to save can have prefix, for example, part-prefix-000-00000.

        Examples:
            .. code-block:: text

                fleet.save_one_table("afs:/user/path/")
        """
        mode = kwargs.get("mode", 0)
        prefix = kwargs.get("prefix", None)
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            if prefix is not None:
                self._fleet_ptr.save_model_one_table_with_prefix(
                    table_id, model_dir, mode, prefix
                )
            else:
                self._fleet_ptr.save_model_one_table(table_id, model_dir, mode)
        self._role_maker._barrier_worker()

    def set_date(self, table_id, date):
        """
        set_date, eg, 20210918
        """
        self._role_maker._barrier_worker()
        if self._role_maker.is_first_worker():
            self._fleet_ptr.set_date(table_id, str(date))
        self._role_maker._barrier_worker()

    def _set_opt_info(self, opt_info):
        """
        this function saves the result from DistributedOptimizer.minimize()
        """
        self._opt_info = opt_info


fleet = PSLib()


def _prepare_params(
    input,
    size,
    is_sparse=False,
    is_distributed=False,
    padding_idx=None,
    param_attr=None,
    dtype='float32',
):
    """
    Preprocess params, this interface is not for users.

    Args:
        input (Variable|list of Variable): Input is a Tensor<int64> Variable.
        size (list of int): The embedding dim.
        is_sparse (bool, optional): Whether input is sparse ids. Default is False.
        is_distributed (bool, optional): Whether in distributed mode. Default is False.
        padding_idx (int, optional): Padding idx of input. Default is None.
        param_attr (ParamAttr, optional): To specify the weight parameter property. Default is None.
        dtype (str, optional): Data type of output. Default is 'float32'.

    """
    if param_attr is None:
        raise ValueError("param_attr must be set")
    name = param_attr.name
    if name is None:
        raise ValueError("embedding name must be set")
    if not isinstance(size, list) and not isinstance(size, tuple):
        raise ValueError("embedding size must be list or tuple")
    size = size[-1]
    global FLEET_GLOBAL_DICT
    FLEET_GLOBAL_DICT["enable"] = True
    d_table = FLEET_GLOBAL_DICT["emb_to_table"]
    d_accessor = FLEET_GLOBAL_DICT["emb_to_accessor"]
    d_size = FLEET_GLOBAL_DICT["emb_to_size"]

    # check embedding size
    if d_size.get(name) is None:
        d_size[name] = size
    elif d_size[name] != size:
        raise ValueError(f"embedding size error: {size} vs {d_size[name]}")

    # check embedding accessor
    accessor = FLEET_GLOBAL_DICT["cur_accessor"]
    if d_accessor.get(name) is None:
        d_accessor[name] = accessor
    elif d_accessor[name] != accessor:
        raise ValueError(
            f"embedding size error: {d_accessor[name]} vs {accessor}"
        )

    # check embedding table id
    if d_table.get(name) is None:
        d_table[name] = FLEET_GLOBAL_DICT["cur_sparse_id"]
        FLEET_GLOBAL_DICT["cur_sparse_id"] += 1

    # check other params
    if not is_sparse:
        raise ValueError("is_sparse must be True")
    elif not is_distributed:
        raise ValueError("is_distributed must be True")
    elif dtype != "float32":
        raise ValueError("dtype must be float32")


def _fleet_embedding(
    input,
    size,
    is_sparse=False,
    is_distributed=False,
    padding_idx=None,
    param_attr=None,
    dtype='float32',
):
    """
    Add fleet embedding, this interface is not for users.

    Args:
        input (Variable|list of Variable): Input is a Tensor<int64> Variable.
        size (list[int]): The embedding dim.
        is_sparse (bool, optional): Whether input is sparse ids. Default is False.
        is_distributed (bool, optional): Whether in distributed mode. Default is False.
        padding_idx (int, optional): Padding idx of input. Default is None.
        param_attr (ParamAttr, optional): To specify the weight parameter property. Default is None.
        dtype (str, optional): Data type of output. Default is 'float32'.
    """

    def _pull_sparse(
        input,
        size,
        table_id,
        accessor_class,
        name="embedding",
        ctr_label_name="",
        padding_id=0,
        dtype='float32',
        scale_sparse_grad=True,
    ):
        helper = LayerHelper(name, **locals())
        inputs = helper.multiple_input()
        outs = [helper.create_variable_for_type_inference(dtype)]
        input_names = [i.name for i in inputs]
        attrs = {
            'EmbeddingDim': size,
            'TableId': table_id,
            'AccessorClass': accessor_class,
            'CtrLabelName': ctr_label_name,
            'PaddingId': padding_id,
            'ScaleSparseGrad': scale_sparse_grad,
            'InputNames': input_names,
            # this is only for compatible with embedding op
            'is_distributed': True,
        }
        # this is only for compatible with embedding op
        w, _ = helper.create_or_get_global_variable(
            name=name,
            shape=[size],
            dtype=dtype,
            is_bias=False,
            persistable=True,
        )
        helper.append_op(
            type='pull_sparse',
            inputs={'Ids': inputs, 'W': w},
            outputs={'Out': outs},
            attrs=attrs,
        )
        if len(outs) == 1:
            return outs[0]
        return outs

    # check and set params
    _prepare_params(
        input, size, is_sparse, is_distributed, padding_idx, param_attr, dtype
    )
    name = param_attr.name
    size = size[-1]
    if padding_idx is None:
        padding_idx = 0
    global FLEET_GLOBAL_DICT

    return _pull_sparse(
        input=input,
        size=size,
        table_id=FLEET_GLOBAL_DICT["emb_to_table"][name],
        accessor_class=FLEET_GLOBAL_DICT["emb_to_accessor"][name],
        name=name,
        ctr_label_name=FLEET_GLOBAL_DICT["click_name"],
        padding_id=padding_idx,
        dtype=dtype,
        scale_sparse_grad=FLEET_GLOBAL_DICT["scale_sparse_grad"],
    )


def _fleet_embedding_v2(
    input,
    size,
    is_sparse=False,
    is_distributed=False,
    padding_idx=None,
    param_attr=None,
    dtype='float32',
):
    """
    Add fleet embedding v2, this interface is not for users.

    Args:
        input (Variable|list of Variable): Input is a Tensor<int64> Variable.
        size (list[int]): The embedding dim.
        is_sparse (bool, optional): Whether input is sparse ids. Default is False.
        is_distributed (bool, optional): Whether in distributed mode. Default is False.
        padding_idx (int, optional): Padding idx of input. Default is None.
        param_attr (ParamAttr, optional): To specify the weight parameter property. Default is None.
        dtype (str, optional): Data type of output. Default is 'float32'.
    """

    def _pull_sparse_v2(
        input,
        size,
        table_id,
        accessor_class,
        name="embedding",
        ctr_label_name="",
        padding_id=0,
        dtype='float32',
        scale_sparse_grad=True,
    ):
        helper = LayerHelper(name, **locals())
        inputs = helper.multiple_input()
        outs = [helper.create_variable_for_type_inference(dtype)]
        input_names = [i.name for i in inputs]
        attrs = {
            'EmbeddingDim': size,
            'TableId': table_id,
            'AccessorClass': accessor_class,
            'CtrLabelName': ctr_label_name,
            'PaddingId': padding_id,
            'ScaleSparseGrad': scale_sparse_grad,
            'InputNames': input_names,
            # this is only for compatible with embedding op
            'is_distributed': True,
        }
        # this is only for compatible with embedding op
        w, _ = helper.create_or_get_global_variable(
            name=name,
            shape=[size],
            dtype=dtype,
            is_bias=False,
            persistable=True,
        )
        helper.append_op(
            type='pull_sparse_v2',
            inputs={'Ids': inputs, 'W': w},
            outputs={'Out': outs},
            attrs=attrs,
        )
        if len(outs) == 1:
            return outs[0]
        return outs

    # check and set params
    _prepare_params(
        input, size, is_sparse, is_distributed, padding_idx, param_attr, dtype
    )
    name = param_attr.name
    size = size[-1]
    if padding_idx is None:
        padding_idx = 0

    return _pull_sparse_v2(
        input=input,
        size=size,
        table_id=FLEET_GLOBAL_DICT["emb_to_table"][name],
        accessor_class=FLEET_GLOBAL_DICT["emb_to_accessor"][name],
        name=name,
        ctr_label_name=FLEET_GLOBAL_DICT["click_name"],
        padding_id=padding_idx,
        dtype=dtype,
        scale_sparse_grad=FLEET_GLOBAL_DICT["scale_sparse_grad"],
    )


class fleet_embedding:
    """
    Fleet embedding class, it is used as a wrapper.

    Examples:

        .. code-block:: text

            with fleet_embedding(click_name=label.name):
                emb = paddle.static.nn.embedding(
                    input=var,
                    size=[-1, 11],
                    is_sparse=True,
                    is_distributed=True,
                    param_attr=base.ParamAttr(name="embedding"))
    """

    def __init__(self, click_name, scale_sparse_grad=True):
        """Init."""
        self.origin_emb_v2 = paddle.static.nn.embedding
        # if user uses cvm layer after embedding, click_name can be None
        self.click_name = "" if click_name is None else click_name
        self.scale_sparse_grad = scale_sparse_grad
        # it's default value, will be modified in minimize
        self.accessor = "DownpourCtrAccessor"

    def __enter__(self):
        """Enter."""
        paddle.static.nn.embedding = _fleet_embedding_v2
        FLEET_GLOBAL_DICT["cur_accessor"] = self.accessor
        FLEET_GLOBAL_DICT["click_name"] = self.click_name
        FLEET_GLOBAL_DICT["scale_sparse_grad"] = self.scale_sparse_grad

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit."""
        paddle.static.nn.embedding = self.origin_emb_v2
        FLEET_GLOBAL_DICT["cur_accessor"] = ""
        FLEET_GLOBAL_DICT["click_name"] = ""
        FLEET_GLOBAL_DICT["scale_sparse_grad"] = None


class DownpourOptimizer(DistributedOptimizer):
    """
    DistributedOptimizer is a wrapper for paddle.base.optimizer
    A user should pass a paddle.base.optimizer to DistributedOptimizer
    minimize() function is implemented.
    DistributedOptimizer is the starting point for a user who wants to
    run distributed training. The optimized information will be stored in
    Fleet() instance who holds the global information about current distributed
    training.

    Args:
        optimizer(Optimizer): subclass of Optimizer.
        strategy(any): config for DownpourOptimizer.

    Returns:
        None
    """

    def __init__(self, optimizer, strategy=None):
        super().__init__(optimizer, strategy)

        self._optimizer = optimizer
        self._optimizer_name = "Distributed%s" % optimizer.type.capitalize()
        if optimizer.type != "adam":
            print(
                "Currently, distributed optimizer only support Adam"
                "Will config built-in adam for you."
                "We will support more functions in DistributedOptimizer",
                sys.stderr,
            )
            self._optimizer_name = "DistributedAdam"

        self._distributed_optimizer = globals()[self._optimizer_name](optimizer)

    def backward(
        self,
        loss,
        startup_program=None,
        parameter_list=None,
        no_grad_set=None,
        callbacks=None,
    ):
        """
        Currently, backward function can not be called through DistributedOptimizer
        """
        raise NotImplementedError()

    def _remove_collective_ops(self, program, name):
        """
        colective init op should call once, so remove other call.
        """
        block = program.global_block()
        for ids, op in list(enumerate(block.ops)):
            if op.type == name:
                block._remove_op(ids)
                return

    def apply_gradients(self, params_grads):
        """
        Currently, apply_gradients function can not be called through DistributedOptimizer
        """
        raise NotImplementedError()

    def get_dist_env(self):
        trainer_id = int(os.getenv('PADDLE_TRAINER_ID', '0'))
        trainer_endpoints = ''
        current_endpoint = ''
        num_trainers = 0
        if os.getenv('PADDLE_TRAINER_ENDPOINTS') and os.getenv(
            'PADDLE_CURRENT_ENDPOINT'
        ):
            trainer_endpoints = os.getenv('PADDLE_TRAINER_ENDPOINTS')
            current_endpoint = os.getenv('PADDLE_CURRENT_ENDPOINT')
            num_trainers = len(trainer_endpoints.split(','))

        return {
            'trainer_id': trainer_id,
            'num_trainers': num_trainers,
            'current_endpoint': current_endpoint,
            'trainer_endpoints': trainer_endpoints,
        }

    def _remove_collective_op_for_embedding(self, loss, table_name):
        """
        find multi-sparse-table
        """
        table_name = [name + "@GRAD" for name in table_name]
        need_remove_op_index = []
        block = loss.block.program.global_block()
        collective_ops = ["c_sync_calc_stream", "c_allreduce_sum"]
        for ids, op in list(enumerate(block.ops)):
            if op.type in collective_ops:
                if op.input("X")[0] in table_name:
                    need_remove_op_index.append(ids)
            if op.type == "lookup_table_grad":
                need_remove_op_index.append(ids)
            try:
                if op.output("Out")[0] in table_name:
                    need_remove_op_index.append(ids)
            except:
                pass

        need_remove_op_index.sort(reverse=True)
        for index in need_remove_op_index:
            block._remove_op(index)

    def minimize(
        self,
        losses,
        scopes=None,
        startup_programs=None,
        parameter_list=None,
        no_grad_set=None,
        program_mode="all_reduce",
    ):
        """
        Minimize a program through loss, loss can be a list in DistributedOptimizer.
        Note that in parameter server mode, a worker will not get anything about optimize_os
        Because optimizer algorithms run on pserver side. We will make this usable in pserver
        process, but currently the optimization part is written into Fleet(). A user does not
        need to care about how to startup a pserver node.

        Args:
            losses (Variable|Variable List): Loss variable or loss variable list to run optimization.
            scopes (Scope|Scope List, Optional): Scope instance. Default is None.
            startup_programs (Program|Program List, Optional): Startup_program for initializing parameters
                in `parameter_list`. Default is None.
            parameter_list (list, Optional): List of Variables to update. Default is None.
            no_grad_set (set, Optional): Set of Variables should be ignored. Default is None.
            program_mode (str, Optional): Grad action for grogram when use_ps_gpu. Default is "all_reduce".

        Returns:
            tuple: (optimize_ops, params_grads) which are, list of operators appended;
                and list of (param, grad) Variables pair for optimization.
        """

        if not isinstance(losses, list):
            losses = [losses]

        (
            optimize_ops,
            param_grads,
            opt_info,
        ) = self._distributed_optimizer._minimize(
            losses,
            startup_programs,
            parameter_list,
            no_grad_set,
            self._strategy,
        )
        opt_info["mpi_rank"] = fleet.worker_index()
        opt_info["mpi_size"] = fleet.worker_num()
        fleet._set_opt_info(opt_info)

        programs = [loss.block.program for loss in losses]

        if scopes is None:
            scopes = [paddle.static.global_scope()] * len(programs)

        if len(scopes) != len(programs):
            raise ValueError(
                "You should make sure len(scopes) == len(programs) or set scopes None"
            )

        fleet._main_programs = programs
        fleet._scopes = scopes
        if opt_info["use_ps_gpu"]:
            from paddle.distributed.transpiler.collective import MultiThread

            # check start program
            if program_mode not in [
                "all_reduce",
                "fuse_all_reduce",
                "all_gather",
                "all_reduce_xpu",
            ]:
                raise ValueError(
                    "You should set program_mode in [ all_reduce, \
                                fuse_all_reduce, all_gather, all_reduce_xpu ]"
                )
            env = self.get_dist_env()
            if not isinstance(losses, list):
                startup_programs = [startup_programs]
            for i in range(0, len(startup_programs)):
                t = MultiThread(trans_mode=program_mode)
                start_program = startup_programs[i]
                main_program = programs[i]
                t.transpile(
                    startup_program=start_program,
                    main_program=main_program,
                    rank=env["trainer_id"],
                    endpoints=env["trainer_endpoints"],
                    current_endpoint=env['current_endpoint'],
                    wait_port=False,
                )
                if i > 0:
                    self._remove_collective_ops(
                        start_program, "c_comm_init_all"
                    )
            for i in range(0, len(losses)):
                loss = losses[i]
                embedding_table = self._distributed_optimizer._find_multi_distributed_lookup_table(
                    [loss]
                )
                self._remove_collective_op_for_embedding(loss, embedding_table)

        return [optimize_ops, param_grads]
