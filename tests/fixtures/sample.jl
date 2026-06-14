module SampleModule

using LinearAlgebra
using Statistics: mean, std
import Base: show, print
import JSON
import DataFrames as DF

export greet, Dog, process
public square, add

@enum Color RED BLUE GREEN

abstract type AbstractAnimal end

struct Dog <: AbstractAnimal
    name::String
    age::Int
end

mutable struct MutablePoint
    x::Float64
    y::Float64
end

function greet(name::String)
    println("Hello, $name")
end

function Base.show(io::IO, d::Dog)
    print(io, "Dog($(d.name))")
end

add(a, b) = a + b

square(x) = x^2

function bar end

Base.length(d::Dog) = d.age

function analyze(v::Vector{Float64})
    return LinearAlgebra.norm(v)
end

const FloatVec = Vector{Float64}

const MY_CONST = 42

macro sayhello(name)
    :(println("Hello, ", $name))
end

function outer()
    function inner()
        return 1
    end
    x = inner()
    result = map(v -> v^2, [1,2,3])
    return x
end

function process(data::Vector{Float64}; verbose=false)
    if verbose
        println("Processing...")
    end
    normed = data ./ maximum(data)
    return sum(normed) / length(normed)
end

include("utils.jl")

@testset "Arithmetic" begin
    @test add(1, 2) == 3
    @test square(4) == 16
end

end # module
