// sample.sv - SystemVerilog fixture for parser tests
`timescale 1ns / 1ps

// File-level package import
import utils_pkg::*;

// Interface declaration
interface BusIf #(parameter int WIDTH = 8);
    logic [WIDTH-1:0] data;
    logic             valid;
    logic             ready;
    modport master(output data, valid, input ready);
    modport slave(input data, valid, output ready);
endinterface

// Submodule to be instantiated by FIFOController
module Adder #(parameter int WIDTH = 8) (input logic [WIDTH-1:0] a, b, output logic [WIDTH-1:0] sum);
    assign sum = a + b;
endmodule

// Main module with tasks, functions, always blocks, and module instantiation
// Parameters on one line to avoid grammar parse errors
module FIFOController #(parameter int DEPTH = 16, parameter int WIDTH = 8) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic [WIDTH-1:0] data_in,
    input  logic             wr_en,
    input  logic             rd_en,
    output logic [WIDTH-1:0] data_out,
    output logic             full,
    output logic             empty
);

    // Intra-module package import
    import arith_pkg::counter_t;

    logic [WIDTH-1:0]       mem [0:DEPTH-1];
    logic [$clog2(DEPTH):0] wr_ptr, rd_ptr, count;
    wire                    overflow_flag;
    localparam int          ALMOST_FULL = DEPTH - 1;

    // Module instantiation - creates CALLS edge from FIFOController to Adder
    Adder #(.WIDTH(WIDTH)) ptr_adder (.a(wr_ptr[WIDTH-1:0]), .b(rd_ptr[WIDTH-1:0]), .sum());

    // Task declaration
    task automatic do_write(input logic [WIDTH-1:0] din);
        mem[wr_ptr] <= din;
        wr_ptr <= wr_ptr + 1;
        count  <= count + 1;
    endtask

    // Function declaration
    function automatic logic is_full();
        return (count >= DEPTH);
    endfunction

    // Always block (sequential logic) - flattened to avoid nested begin/end
    // grammar limitation: if(x) begin..end inside else begin..end causes parse errors
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= 0;
            rd_ptr <= 0;
            count  <= 0;
        end
        if (rst_n && wr_en && !full)  do_write(data_in);
        if (rst_n && rd_en && !empty) begin
            data_out <= mem[rd_ptr];
            rd_ptr   <= rd_ptr + 1;
            count    <= count - 1;
        end
    end

    // Always block (combinational logic)
    always_comb begin
        full  = is_full();
        empty = (count == 0);
    end

endmodule

// Top-level wrapper: multi-module hierarchy with wire feedthrough.
// stage_data/stage_valid carry a signal from the Adder output into the
// FIFOController input. Instantiation port maps kept single-line (grammar
// is brittle on multi-line connections).
module Top #(parameter int WIDTH = 8) (input logic clk, input logic rst_n, input logic [WIDTH-1:0] din, output logic [WIDTH-1:0] dout);
    wire [WIDTH-1:0] stage_data;
    wire             stage_valid;
    localparam int   STAGES = 2;
    Adder #(.WIDTH(WIDTH)) u_add (.a(din), .b(din), .sum(stage_data));
    FIFOController #(.WIDTH(WIDTH)) u_fifo (.clk(clk), .rst_n(rst_n), .data_in(stage_data), .wr_en(stage_valid), .rd_en(stage_valid), .data_out(dout), .full(), .empty());
endmodule
