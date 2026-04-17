import Foundation

protocol UserRepository {
    func findById(_ id: Int) -> User?
    func save(_ user: User)
}

struct User {
    let id: Int
    let name: String
    let email: String
}

class InMemoryRepo: UserRepository {
    private var users: [Int: User] = [:]

    func findById(_ id: Int) -> User? {
        return users[id]
    }

    func save(_ user: User) {
        users[user.id] = user
        print("Saved user \(user.id)")
    }
}

enum Direction: String {
    case north
    case south
    case east
    case west
}

actor DataStore {
    private var cache: [String: User] = [:]

    func get(_ key: String) -> User? {
        return cache[key]
    }

    func set(_ key: String, user: User) {
        cache[key] = user
    }
}

extension InMemoryRepo: CustomStringConvertible {
    var description: String {
        return "InMemoryRepo with \(users.count) users"
    }

    func clear() {
        users.removeAll()
    }
}

func createUser(repo: UserRepository, name: String, email: String) -> User {
    let user = User(id: 1, name: name, email: email)
    repo.save(user)
    return user
}
