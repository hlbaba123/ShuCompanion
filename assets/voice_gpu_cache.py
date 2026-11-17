"""Retain autoregressive ONNX cache tensors on their CUDA device."""


class DeviceCacheSession:
    def __init__(self,session,ort_value_type):
        self._session=session;self._value_type=ort_value_type

    def __getattr__(self,name):return getattr(self._session,name)

    def run(self,output_names,feeds):
        if output_names is not None:raise ValueError('Cache binding needs all outputs')
        binding=self._session.io_binding()
        for name,value in feeds.items():
            if isinstance(value,self._value_type):binding.bind_ortvalue_input(name,value)
            else:binding.bind_cpu_input(name,value)
        names=[output.name for output in self._session.get_outputs()]
        for name in names:binding.bind_output(name,'cuda' if name.startswith('present_') else 'cpu')
        self._session.run_with_iobinding(binding)
        values=binding.get_outputs()
        # The small hidden state drives the local decoder. KV tensors pass
        # unchanged to the next step without a GPU→CPU→GPU copy every frame.
        return [value if name.startswith('present_') else value.numpy() for name,value in zip(names,values)]
