defmodule Calculator do
  @moduledoc """
  Simple calculator module.
  """

  def add(a, b) do
    a + b
  end

  def subtract(a, b), do: a - b

  defp log(msg) do
    IO.puts(msg)
    :ok
  end

  def compute(a, b) do
    result = add(a, b)
    log("result: #{result}")
    result
  end
end

defmodule MathHelpers do
  alias Calculator
  import Calculator, only: [add: 2]
  require Logger

  def double(x) do
    Calculator.compute(x, x)
  end

  def triple(x) do
    double(x) + x
  end
end
