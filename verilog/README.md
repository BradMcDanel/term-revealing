This code contains the verilog implementation for the Term revealing system. All the evaluation results are generated with the Xilinx VC707 evaluation board.

systolic_dla_top.v: the top level file of the term-revealing system.

systolic_array.v: shows the implementation of a 64 by 128 systolic array, where each systolic cell is a tMAC. 

coe_acc.v : verilog implementation of coefficient accumulator.

relu_quantizer.v: verilog implementation for the ReLU block. 

hese_encoder.v: verilog implementation for the hese_encoder.

binary_stream_converter.v: verilog implementation for the binary stream converter.

concatenator_truncator.v: verilog implementation of term comparator.

