#!/bin/sh

thisUser=$MY_USER
thisGroup=$MY_GROUP

# create the passwd and group mount point dynamically at runtime
passwdEntry=$(getent passwd $thisUser | awk -F : -v thisHome="/home/$thisUser" '{print $1 ":" $2 ":" $3 ":" $4 ":" $5 ":" thisHome ":" $7}')
groupEntry=$(getent group $thisGroup)

echo $HOST_MOUNT_DIR

# workaround case where Unix account is not in the local system (e.g. sssd)
[[ -d $HOST_MOUNT_DIR/admin/etc/ ]] || (mkdir -p $HOST_MOUNT_DIR/admin/etc) || exit $?
[[ -f $HOST_MOUNT_DIR/admin/etc/passwd ]] || {
    echo "Creating passwd file"
    getent passwd > $HOST_MOUNT_DIR/admin/etc/passwd
    echo $passwdEntry >> $HOST_MOUNT_DIR/admin/etc/passwd
}
[[ -f $HOST_MOUNT_DIR/admin/etc/group ]] || {
    echo "Creating group file"
    getent group > $HOST_MOUNT_DIR/admin/etc/group
    echo $groupEntry >> $HOST_MOUNT_DIR/admin/etc/group
}