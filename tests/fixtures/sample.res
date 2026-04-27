// sample.res - Comprehensive ReScript test fixture
// Exercises modules, nested modules, let/rec, externals, types, opens,
// decorators, function calls, and test-style bindings.

open Belt
include Js.Promise
open Belt

// Module alias (re-export)
module IntMap = Belt.Map.Int

// JS-binding module: only types + externals, should be tagged js_binding
module TextEncoder = {
  type encoder
  @new external newTextEncoder: unit => encoder = "TextEncoder"
  @send external encode: (encoder, string) => array<int> = "encode"
}

// Top-level type definition
type status = Active | Inactive | Pending

// Top-level type alias with polymorphic parameter
type result<'a> = Ok('a) | Err(string)

// Top-level let binding
let defaultTimeout = 5000

// let rec + and chain
let rec fact = n => n <= 1 ? 1 : n * fact(n - 1)
and helper = x => fact(x) + 1

// External binding with decorator
@module("fs") external readFile: string => string = "readFileSync"
@val external consoleLog: string => unit = "console.log"

// Nested module
module User = {
  type t = {name: string, age: int, status: status}

  let make = (~name, ~age) => {name, age, status: Active}

  let greet = (user: t) => consoleLog("Hello " ++ user.name)

  // Nested sub-module
  module Validator = {
    let isAdult = (user: t) => user.age >= 18
    let hasName = (user: t) => user.name != ""
  }
}

// Another top-level module using the previous one
module App = {
  let start = () => {
    let u = User.make(~name="Ada", ~age=36)
    User.greet(u)
    let valid = User.Validator.isAdult(u)
    consoleLog(valid ? "ok" : "nope")
  }
}

// Top-level function calling into modules
let main = () => {
  App.start()
  let n = fact(5)
  consoleLog(Belt.Int.toString(n))
}

// JSX rendering — component references across modules
let render = () =>
  <Layout>
    <User.Badge name="Ada" />
    <AnalyticsFilterUi.Filter filter="amount" />
  </Layout>

// Test-style function (rescript-test convention)
let test_fact_base = () => {
  let r = fact(1)
  assert(r == 1)
}
