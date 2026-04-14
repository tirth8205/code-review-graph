#!/bin/ksh
# Sample Korn shell script exercising the bash parser via the .ksh extension.

source ./sample_lib.sh

backup_logs() {
    echo "archiving"
    tar czf backup.tgz /var/log
}

rotate() {
    backup_logs
    echo "done"
}

rotate
