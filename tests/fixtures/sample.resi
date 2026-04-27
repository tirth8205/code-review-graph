/* sample.resi - ReScript interface file fixture.
   Only signatures — no expression bodies. */

type status = Active | Inactive | Pending

type result<'a> = Ok('a) | Err(string)

let defaultTimeout: int

let fact: int => int

module User: {
  type t
  let make: (~name: string, ~age: int) => t
  let greet: t => unit

  module Validator: {
    let isAdult: t => bool
    let hasName: t => bool
  }
}

module App: {
  let start: unit => unit
}

external readFile: string => string = "readFileSync"
