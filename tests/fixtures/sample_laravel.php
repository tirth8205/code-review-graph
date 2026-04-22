<?php

namespace App\Http\Controllers;

use App\Models\Post;
use App\Models\User;

class UserController extends Controller
{
    public function index(): array
    {
        return User::all()->toArray();
    }

    public function show(int $id): ?User
    {
        return User::find($id);
    }
}

// --- Route definitions ---

Route::get('/users', [UserController::class, 'index']);
Route::post('/users', [UserController::class, 'store']);
Route::get('/users/{id}', [UserController::class, 'show']);

// --- Eloquent Model with relationships ---

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class User extends Model
{
    public function posts(): HasMany
    {
        return $this->hasMany(Post::class);
    }

    public function comments()
    {
        return $this->hasMany(Comment::class);
    }
}

class Post extends Model
{
    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }

    public function tags()
    {
        return $this->belongsToMany(Tag::class);
    }
}

// --- Service Provider ---

class AppServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        // service bindings
    }

    public function boot(): void
    {
        // bootstrapping
    }
}

// --- Artisan Command ---

class SendEmails extends Command
{
    public function handle(): void
    {
        // command logic
    }
}
