// Mocha TDD interface: enabled via `mocha --ui tdd`.
// `suite` is the describe-equivalent and `test` is the it-equivalent.
import { UserRepository, UserService } from './sample_typescript';

suite('UserService (mocha TDD)', () => {
  test('constructs a service', () => {
    const service = new UserService();
    if (!service) throw new Error('expected service');
  });

  test('returns undefined for unknown id', () => {
    const service = new UserService();
    const user = service.getUser(404);
    if (user !== undefined) throw new Error('expected undefined');
  });
});
