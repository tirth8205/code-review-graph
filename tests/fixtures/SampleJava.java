package com.example.auth;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public interface UserRepository {
    Optional<User> findById(int id);
    void save(User user);
}

class User {
    private int id;
    private String name;
    private String email;

    public User(int id, String name, String email) {
        this.id = id;
        this.name = name;
        this.email = email;
    }

    public int getId() { return id; }
    public String getName() { return name; }
    public String getEmail() { return email; }
}

class InMemoryRepo implements UserRepository {
    private Map<Integer, User> users = new HashMap<>();

    @Override
    public Optional<User> findById(int id) {
        return Optional.ofNullable(users.get(id));
    }

    @Override
    public void save(User user) {
        users.put(user.getId(), user);
        System.out.println("Saved user " + user.getId());
    }
}

class UserService {
    private final UserRepository repo;

    public UserService(UserRepository repo) {
        this.repo = repo;
    }

    public User createUser(String name, String email) {
        User user = new User(1, name, email);
        repo.save(user);
        return user;
    }

    public Optional<User> getUser(int id) {
        return repo.findById(id);
    }
}

class CachedRepo extends InMemoryRepo {
    @Override
    public void save(User user) {
        super.save(user);
    }
}
