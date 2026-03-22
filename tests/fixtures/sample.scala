package com.example.auth

import scala.collection.mutable
import scala.collection.mutable.{HashMap, ListBuffer}
import scala.util.Try
import scala.concurrent._

trait Repository[T]:
  def findById(id: Int): Option[T]
  def save(entity: T): Unit

case class User(id: Int, name: String, email: String)

class InMemoryRepo extends Repository[User] with Serializable:
  private val users = mutable.HashMap[Int, User]()

  override def findById(id: Int): Option[User] =
    users.get(id)

  override def save(user: User): Unit =
    users.put(user.id, user)
    println(s"Saved user ${user.id}")

class UserService(repo: Repository[User]):
  def createUser(name: String, email: String): User =
    val user = User(1, name, email)
    repo.save(user)
    user

  def getUser(id: Int): Option[User] =
    repo.findById(id)

object UserService:
  def apply(repo: Repository[User]): UserService = new UserService(repo)

enum Color:
  case Red, Green, Blue
