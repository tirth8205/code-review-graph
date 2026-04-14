const path = require('path');
const { cn } = require('./src/lib/utils');

function main() {
    const dir = path.resolve('.');
    const cls = cn('foo', 'bar');
    return dir + cls;
}
