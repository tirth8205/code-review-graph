package com.example.service

import com.example.auth.UserRepository
import com.example.auth.User
import com.example.auth.InMemoryRepo

class AccountService(private val repo: UserRepository) {
    fun createAccount(name: String, email: String): User {
        val user = User(1, name, email)
        repo.save(user)
        return user
    }
}

fun main() {
    val repo = InMemoryRepo()
    val service = AccountService(repo)
    val user = service.createAccount("Alice", "alice@example.com")
    println(user)
}
