import 'dart:async';
import 'package:flutter/material.dart';

abstract class Animal {
  String get name;
  void speak();
}

mixin SwimmingMixin {
  void swim() => print('swimming');
}

enum PetType { dog, cat, bird }

class Dog extends Animal with SwimmingMixin {
  final String name;
  final PetType type;

  Dog(this.name) : type = PetType.dog;

  @override
  void speak() {
    print('Woof! I am $name');
  }

  Future<void> fetch(String item) async {
    await _run();
    print('Fetched $item');
  }

  void _run() {
    print('running');
  }

  static Dog create(String name) {
    return Dog(name);
  }
}

Dog createDog(String name) {
  return Dog(name);
}
