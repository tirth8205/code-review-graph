import { describe, it, expect } from 'vitest';
import { UserRepository, UserService } from '../sample_typescript';

describe('UserService (under __tests__/)', () => {
  it('constructs a service', () => {
    const service = new UserService();
    expect(service).toBeDefined();
  });

  it('returns undefined for missing user', () => {
    const service = new UserService();
    const user = service.getUser(999);
    expect(user).toBeUndefined();
  });
});
