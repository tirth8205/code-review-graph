@extends('layouts.app')

@section('title', 'Home')

@section('content')
    <div class="container">
        <h1>Welcome</h1>

        @include('partials.header')

        @component('components.alert')
            <strong>Attention!</strong> Something important.
        @endcomponent

        @include('partials.footer')

        @livewire('components.counter')
    </div>
@endsection
