package com.example.service;

import com.example.auth.UserService;
import com.example.auth.User;
import java.util.Optional;

public class AccountController {
    private final UserService userService;

    public AccountController(UserService userService) {
        this.userService = userService;
    }

    public User createAccount(String name, String email) {
        return userService.createUser(name, email);
    }

    public Optional<User> getAccount(int id) {
        return userService.getUser(id);
    }
}
