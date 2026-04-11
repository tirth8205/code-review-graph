/**
 * Fixture: JSX attribute function references.
 *
 * Pain point: `<Button onClick={handleDelete} />` does NOT emit a CALLS edge
 * because _walk_func_ref_args only scans argument_list nodes, not jsx_expression.
 */

import React from 'react';

function handleDelete() {
  console.log('deleted');
}

function handleChange(e: any) {
  console.log(e.target.value);
}

function handleSubmit() {
  console.log('submitted');
}

export function MyComponent() {
  return (
    <div>
      <button onClick={handleDelete}>Delete</button>
      <input onChange={handleChange} />
      <form onSubmit={handleSubmit}>
        <button type="submit">Submit</button>
      </form>
    </div>
  );
}
