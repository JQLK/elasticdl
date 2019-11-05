import threading

from google.protobuf import empty_pb2

from elasticdl.proto import elasticdl_pb2, elasticdl_pb2_grpc
from elasticdl.python.common.dtypes import dtype_numpy_to_tensor
from elasticdl.python.common.log_utils import default_logger as logger
from elasticdl.python.common.tensor import Tensor, serialize_tensor


class PserverServicer(elasticdl_pb2_grpc.PserverServicer):
    """PS service implementation"""

    def __init__(
        self,
        parameters,
        grads_to_wait,
        optimizer,
        lr_staleness_modulation=False,
        use_async=False,
    ):
        self._parameters = parameters
        self._grads_to_wait = grads_to_wait
        self._optimizer = optimizer
        self._lr_staleness_modulation = lr_staleness_modulation
        self._use_async = use_async
        self._version_lock = threading.Lock()
        self._lock = threading.Lock()

    def pull_variable(self, request, _):
        """
        Response with all non-embedding parameters if initialized.
        """
        res = elasticdl_pb2.PullVariableResponse()
        if not self._parameters.init_status:
            res.model_init_status = False
            return res

        # Only sync-SGD needs lock
        # TODO: use a read-write lock to support multiple concurrent reads
        if not self._use_async:
            self._lock.acquire()
        res.model.version = self._parameters.version
        for name, var in self._parameters.non_embedding_params.items():
            tensor = res.model.param.add()
            tensor.name = name
            tensor.dim.extend(var.shape.as_list())
            var_values = var.numpy()
            tensor.content = var_values.tobytes()
            tensor.dtype = dtype_numpy_to_tensor(var_values.dtype)
        if not self._use_async:
            self._lock.release()
        res.model_init_status = True
        return res

    def pull_embedding_vector(self, request, _):
        ret = elasticdl_pb2.Tensor()
        if not request.ids:
            return ret
        embedding_vectors = self._parameters.get_embedding_param(
            request.name, request.ids
        )
        tensor = Tensor(values=embedding_vectors)
        serialize_tensor(tensor, ret)
        return ret

    def push_model(self, request, _):
        with self._lock:
            self._parameters.init_from_model_pb(request)
        return empty_pb2.Empty()

    def push_gradient(self, request, _):
        if self._use_async:
            grad_vars = []
            for pb in request.gradients:
                tensor = Tensor.from_tensor_pb(pb)
                var = self._parameters.get_non_embedding_param(tensor.name)
                if var is None:
                    logger.warning(
                        "Gradients with invalid name %s" % tensor.name
                    )
                    continue
                grad = tensor.to_tf_tensor()
                grad_vars.append((grad, var))

            self._optimizer.apply_gradients(grad_vars)
            with self._version_lock:
                self._parameters.version += 1

            res = elasticdl_pb2.PushGradientResponse()
            res.accepted = True
            res.model_version = self._parameters.version
            return res

        raise NotImplementedError(
            "Updating parameters synchronously is not implemented."
        )
        return elasticdl_pb2.PushGradientResponse()