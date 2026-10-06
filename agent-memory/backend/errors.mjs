export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export const invalid = message => new ApiError(400, 'invalid_input', message);
export const upstream = () => new ApiError(503, 'upstream_unavailable', 'Memory extraction is unavailable. No memory changes were applied.');

export function stringField(value, name, max, optional = false) {
  if (optional && value === undefined) return '';
  if (typeof value !== 'string' || (!optional && !value.trim()) || value.length > max || value.includes('\u0000')) {
    throw invalid(`${name} must be ${optional ? 'a' : 'a nonempty'} string of at most ${max} characters.`);
  }
  return value;
}
