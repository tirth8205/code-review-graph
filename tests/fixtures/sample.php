<?php

namespace App\Models;

use Exception;
use App\Contracts\{Loggable, Cacheable};
use App\Services\UserService as Service;

trait Timestampable {
    public function getCreatedAt(): string {
        return $this->created_at;
    }
}

enum Status: string {
    case Active = 'active';
    case Inactive = 'inactive';

    public function label(): string {
        return match($this) {
            self::Active => 'Active',
            self::Inactive => 'Inactive',
        };
    }
}

interface Repository {
    public function findById(int $id): ?User;
    public function save(User $user): void;
}

class User {
    use Timestampable;

    public int $id;
    public string $name;

    public function __construct(int $id, string $name) {
        $this->id = $id;
        $this->name = $name;
    }

    public function toString(): string {
        return "User({$this->id}, {$this->name})";
    }

    public static function find(int $id): ?self {
        return null;
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

class UserController extends Controller implements Loggable {
    public function index(): array {
        $users = User::find(1);
        $repo = new InMemoryRepo();
        $repo->save(new User(1, 'Alice'));
        return [];
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
