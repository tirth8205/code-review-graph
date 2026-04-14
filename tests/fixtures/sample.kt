package com.example

import java.util.UUID

interface UserRepository {
    fun findById(id: Int): User?
    fun save(user: User)
}

data class User(val id: Int, val name: String, val email: String)

class InMemoryRepo : UserRepository {
    private val users = mutableMapOf<Int, User>()

    override fun findById(id: Int): User? = users[id]

    override fun save(user: User) {
        users[user.id] = user
        println("Saved user ${user.id}")
    }
}

fun createUser(repo: UserRepository, name: String, email: String): User {
    val user = User(1, name, email)
    repo.save(user)
    return user
}

object UserFactory {
    fun create(name: String): User = User(1, name, "$name@example.com")
}

fun main() {
    val user = UserFactory.create("Alice")
    println(user)
}
