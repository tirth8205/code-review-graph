#pragma once
#include <string>

class Shape {
public:
    std::string color;

    Shape(std::string c) : color(c) {}
    virtual double area() const = 0;
};

class Circle : public Shape {
public:
    double radius;

    Circle(std::string c, double r) : Shape(c), radius(r) {}
    double area() const override { return 3.14159 * radius * radius; }
};

inline double perimeter(const Circle& circle) {
    return 2.0 * 3.14159 * circle.radius;
}
