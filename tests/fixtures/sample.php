<?php

namespace App\Models;

use Exception;

interface Repository {
    public function findById(int $id): ?User;
    public function save(User $user): void;
}

class User {
    public int $id;
    public string $name;

    public function __construct(int $id, string $name) {
        $this->id = $id;
        $this->name = $name;
    }

    public function toString(): string {
        return "User({$this->id}, {$this->name})";
    }
}

class InMemoryRepo implements Repository {
    private array $users = [];

    public function findById(int $id): ?User {
        return $this->users[$id] ?? null;
    }

    public function save(User $user): void {
        $this->users[$user->id] = $user;
        echo "Saved " . $user->toString() . "\n";
    }
}

function createUser(Repository $repo, string $name): User {
    $user = new User(count($repo->users ?? []) + 1, $name);
    $repo->save($user);
    return $user;
}

function sqlQuery(string $query): array {
    return [];
}

function xl(string $value): string {
    return $value;
}

function text(string $value): string {
    return $value;
}

class SearchService {
    public function search(string $term): array {
        return [];
    }
}

class QueryUtils {
    public static function fetchRecords(): array {
        return [];
    }
}

class EncounterService {
    public static function create(array $payload): bool {
        return true;
    }
}

class ExtendedRepo extends InMemoryRepo {
    public function __construct() {
        parent::__construct();
    }

    public static function factory(): self {
        return new self();
    }

    private function execute(): void {
        // no-op helper used for call extraction coverage
    }

    public function runQueries(?SearchService $service): void {
        sqlQuery("SELECT 1");
        xl("hello");
        text("world");
        $this->execute();
        $service?->search("blood pressure");
        QueryUtils::fetchRecords();
        EncounterService::create([]);
        parent::__construct();
        self::factory();
        \dirname("/tmp");
    }
}
