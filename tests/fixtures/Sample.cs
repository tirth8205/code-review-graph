using System;
using System.Collections.Generic;

namespace SampleApp
{
    public interface IRepository
    {
        User FindById(int id);
        void Save(User user);
    }

    public class User
    {
        public int Id { get; set; }
        public string Name { get; set; }
    }

    public class InMemoryRepo : IRepository
    {
        private Dictionary<int, User> _users = new();

        public User FindById(int id)
        {
            return _users.ContainsKey(id) ? _users[id] : null;
        }

        public void Save(User user)
        {
            _users[user.Id] = user;
            Console.WriteLine($"Saved user {user.Id}");
        }
    }

    public class UserService
    {
        private IRepository _repo;

        public UserService(IRepository repo)
        {
            _repo = repo;
        }

        public User GetUser(int id)
        {
            return _repo.FindById(id);
        }
    }

    // Inheritance coverage for INHERITS edges (base_list clause).
    // Extends a base class AND implements an interface (both bare identifiers).
    public class CachedRepo : InMemoryRepo, IRepository
    {
        public new User FindById(int id)
        {
            return base.FindById(id);
        }
    }

    // Qualified base type name (qualified_name node).
    public class DisposableService : System.IDisposable
    {
        public void Dispose() { }
    }

    // Generic base type name (generic_name node).
    public class UserList : List<User>
    {
    }

    // Nested-qualified generic base (qualified generic_name).
    public class ScopedUserList : System.Collections.Generic.List<User>
    {
    }

    // Generic type parameter constraint — `where T : IRepository` is NOT a base
    // and must NOT produce an INHERITS edge. ConstrainedHolder itself has no base.
    public class ConstrainedHolder<T> where T : IRepository
    {
        public T Value { get; set; }
    }

    // record with a base class + interface (record_declaration must be parsed
    // as a class-like node so its base_list is reached).
    public record AuditedUser : User, IRepository
    {
        public User FindById(int id) { return null; }
        public void Save(User user) { }
    }

    // positional record with a primary-constructor base (drop the (args)).
    public record TaggedUser(int Id, string Tag) : User
    {
    }

    // struct implementing an interface.
    public struct Token : IRepository
    {
        public User FindById(int id) { return null; }
        public void Save(User user) { }
    }
}
