package com.example.app

import scala.collection.mutable
import com.example.utils.{Helper, Logger}

trait Repository[T] {
  def findById(id: String): Option[T]
  def save(entity: T): Unit
}

case class User(name: String, email: String)

class UserService(repo: Repository[User]) extends BaseService with Logging {
  def createUser(name: String, email: String): User = {
    val user = User(name, email)
    repo.save(user)
    Logger.info(s"Created user: $name")
    user
  }

  def getUser(id: String): Option[User] = {
    repo.findById(id)
  }
}

object UserService {
  def apply(repo: Repository[User]): UserService = {
    new UserService(repo)
  }
}
