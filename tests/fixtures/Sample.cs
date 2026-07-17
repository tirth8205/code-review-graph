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

    // Inheritance coverage for C# base_list clauses.
    public class CachedRepo : InMemoryRepo, IRepository
    {
        public new User FindById(int id) { return base.FindById(id); }
    }

    public class DisposableService : System.IDisposable
    {
        public void Dispose() { }
    }

    public class UserList : List<User> { }
    public class ScopedUserList : System.Collections.Generic.List<User> { }

    // A generic constraint is not an inheritance clause.
    public class ConstrainedHolder<T> where T : IRepository
    {
        public T Value { get; set; }
    }

    public record AuditedUser : User, IRepository
    {
        public User FindById(int id) { return null; }
        public void Save(User user) { }
    }

    public record TaggedUser(int Id, string Tag) : User { }

    public struct Token : IRepository
    {
        public User FindById(int id) { return null; }
        public void Save(User user) { }
    }

    // Constructor arguments and enum storage types are not bases.
    public class SeededRepo(int seed) : InMemoryRepo
    {
        public int Seed { get; } = seed;
    }

    public enum Status : byte
    {
        Active,
        Closed,
    }
}
