const std = @import("std");
const util = @import("./sample_zig_util.zig");

pub fn main() !void {
    std.debug.print("hello\n", .{});
    const x = helper(2);
    _ = x;
    util.noop();
}

fn helper(x: i32) i32 {
    return x + 1;
}

pub const Point = struct {
    x: i32,
    y: i32,

    pub fn init(x: i32, y: i32) Point {
        return .{ .x = x, .y = y };
    }

    pub fn distance(self: Point, other: Point) f32 {
        _ = other;
        return @intCast(helper(self.x));
    }
};

const Color = enum { red, green, blue };

pub const Shape = union(enum) {
    circle: f32,
    square: f32,
};

test "helper increments" {
    try expect(helper(1) == 2);
}
