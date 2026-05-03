import { describe, it, test, expect, beforeEach } from 'bun:test';
import { UserRepository, UserService } from './sample_typescript';

describe('UserService (bun)', () => {
  let repo: UserRepository;

  beforeEach(() => {
    repo = new UserRepository();
  });

  it('constructs a service with a repository', () => {
    const service = new UserService();
    expect(service).toBeDefined();
  });

  it('finds a user by id', () => {
    const service = new UserService();
    const user = service.getUser(123);
    expect(user).toBeUndefined();
  });

  test('creates a user via the service', () => {
    const service = new UserService();
    const created = service.createUser('alice', 'alice@example.com');
    expect(created.name).toBe('alice');
  });
});
