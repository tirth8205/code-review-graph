use std::collections::HashMap;

pub trait Repository {
    fn find_by_id(&self, id: u64) -> Option<&User>;
    fn save(&mut self, user: User);
}

#[derive(Debug, Clone)]
pub struct User {
    pub id: u64,
    pub name: String,
    pub email: String,
}

pub struct InMemoryRepo {
    users: HashMap<u64, User>,
}

impl InMemoryRepo {
    pub fn new() -> Self {
        InMemoryRepo {
            users: HashMap::new(),
        }
    }
}

impl Repository for InMemoryRepo {
    fn find_by_id(&self, id: u64) -> Option<&User> {
        self.users.get(&id)
    }

    fn save(&mut self, user: User) {
        println!("Saving user {}", user.id);
        self.users.insert(user.id, user);
    }
}

pub fn create_user(repo: &mut impl Repository, name: &str, email: &str) -> User {
    let user = User {
        id: 1,
        name: name.to_string(),
        email: email.to_string(),
    };
    repo.save(user.clone());
    user
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_repo_is_empty() {
        let repo = InMemoryRepo::new();
        assert!(repo.find_by_id(1).is_none());
    }

    #[test]
    fn create_user_saves_to_repo() {
        let mut repo = InMemoryRepo::new();
        let user = create_user(&mut repo, "alice", "a@b.c");
        assert_eq!(user.name, "alice");
    }

    #[tokio::test]
    async fn async_test_is_detected() {
        assert!(true);
    }
}
