import { UserRepository, UserService } from './sample_typescript';

describe('UserService', () => {
  it('should create a user', () => {
    const repo = new UserRepository();
    const service = new UserService(repo);
  });

  it('should find a user by id', () => {
    const repo = new UserRepository();
    const service = new UserService(repo);
    const user = service.findById('123');
  });

  test('alternative test syntax', () => {
    const repo = new UserRepository();
  });
});
