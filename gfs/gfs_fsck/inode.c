/******************************************************************************
*******************************************************************************
**
**  Copyright (C) 2005 Red Hat, Inc.  All rights reserved.
**
**  This copyrighted material is made available to anyone wishing to use,
**  modify, copy, or redistribute it subject to the terms and conditions
**  of the GNU General Public License v.2.
**
*******************************************************************************
******************************************************************************/

#include "fsck_incore.h"
#include "fsck.h"
#include "osi_user.h"
#include "bio.h"
#include "fs_inode.h"
#include "inode.h"
#include "rgrp.h"
#include "util.h"
#include "fs_dir.h"

/* FIXME: Not crazy about this name vs. load_inode, but I'm not very
 * creative ATM */
/* replaces fs_copyin_dinode */
int copyin_inode(struct fsck_sb *sbp, osi_buf_t *bh, struct fsck_inode **inode)
{
	struct fsck_inode *ip;

	if(!(ip = (struct fsck_inode *)malloc(sizeof(struct fsck_inode)))) {
		log_err("Unable to allocate memory for inode\n");
		return -1;
	}
	if(!memset(ip, 0, sizeof(struct fsck_inode))) {
		log_err("Unable to zero inode memory\n");
		return -1;
	}
	ip->i_sbd = sbp;

	ip->i_num.no_addr = ip->i_num.no_formal_ino = BH_BLKNO(bh);
	memset(&ip->i_di, 0, sizeof(struct gfs_dinode));

	gfs_dinode_in(&ip->i_di, BH_DATA(bh));

	*inode = ip;

	return 0;
}

int load_inode(struct fsck_sb *sbp, uint64_t block, struct fsck_inode **inode)
{
	osi_buf_t *bh;

	if(get_and_read_buf(sbp, block, &bh, 0)){
		stack;
		log_err("Unable to retrieve block %"PRIu64"\n",
			block);
		return -1;
	}

	if(copyin_inode(sbp, bh, inode)) {
		stack;
		relse_buf(sbp, bh);
		return -1;
	}

	relse_buf(sbp, bh);
	return 0;
}


void free_inode(struct fsck_inode **inode)
{
	free(*inode);
	inode = NULL;
}


int check_inode(struct fsck_inode *ip)
{
	int error = 0;
	if(ip->i_di.di_header.mh_type != GFS_METATYPE_DI) {
		return -1;
	}

	if(ip->i_num.no_formal_ino != ip->i_di.di_num.no_formal_ino){
		log_err(
			"In-core and on-disk formal inode"
			"numbers do not match. %"PRIu64" %"PRIu64"\n",
			ip->i_num.no_formal_ino,
			ip->i_di.di_num.no_formal_ino);
		error = -1;
	}

	/*  Handle a moved inode  */

	if (ip->i_num.no_addr != ip->i_di.di_num.no_addr){
		log_err("\tBlock # used to read disk inode: %"PRIu64"\n"
			"\tBlock # recorded in disk inode : %"PRIu64"\n",
			ip->i_num.no_addr, ip->i_di.di_num.no_addr);
		error = -1;
	}

	return error;

}



/*int remove_inode(struct fsck_sb *sbp, uint64_t block)
{
	struct fsck_inode *ip;
	load_inode(sbp, block, &ip);
	check_metatree(ip, &fxns);
	free_inode(&ip);
	return 0;
}*/

/**
 * fs_get_istruct - Get an inode given its number
 * @sdp: The GFS superblock
 * @inum: The inode number
 * @create: Flag to say if we are allowed to create a new struct fsck_inode
 * @ipp: pointer to put the returned inode in
 *
 * Returns: 0 on success, -1 on error
 */
static int fs_get_istruct(struct fsck_sb *sdp, struct gfs_inum *inum,
			  int create, struct fsck_inode **ipp)
{
	struct fsck_inode *ip = NULL;
	int error = 0;
	NEEDS_CHECKING;

	if (!create){
		/* we are not currently tracking which inodes we already have */
		error = -1;
		goto out;
	}

	ip = (struct fsck_inode *)malloc(sizeof(struct fsck_inode));
	ip->i_num = *inum;

	ip->i_sbd = sdp;

	error = fs_copyin_dinode(ip, NULL);
	if (error){
		free(ip);
		ip = NULL;
		goto out;
	}

 out:
	*ipp = ip;

	return error;
}



/**
 * make_dinode - Fill in a new dinode structure
 * @dip: the directory this inode is being created in
 * @inum: the inode number
 * @type: the file type
 * @mode: the file permissions
 * @cred: a credentials structure
 *
 */

static int make_dinode(struct fsck_inode *dip, struct fsck_sb *sdp,
		       struct gfs_inum *inum,
                       unsigned int type, unsigned int mode, osi_cred_t *cred)
{
	struct gfs_dinode di;
	osi_buf_t *dibh;
	struct fsck_rgrp *rgd;
	int error;
	NEEDS_CHECKING;

	error = get_and_read_buf(sdp, inum->no_addr, &dibh, 0);
	if (error)
		goto out;

	if(check_meta(dibh, 0)){
		struct gfs_meta_header mh;
		printf( "make_dinode:  Buffer #%"PRIu64" has no meta header.\n",
			BH_BLKNO(dibh));
		memset(&mh, 0, sizeof(struct gfs_meta_header));
		mh.mh_magic = GFS_MAGIC;
		mh.mh_type = GFS_METATYPE_NONE;
		gfs_meta_header_out(&mh, BH_DATA(dibh));
		printf( "meta header added.\n");
	}

	((struct gfs_meta_header *)BH_DATA(dibh))->mh_type =
		cpu_to_gfs32(GFS_METATYPE_DI);
	((struct gfs_meta_header *)BH_DATA(dibh))->mh_format =
		cpu_to_gfs32(GFS_FORMAT_DI);

	memset(BH_DATA(dibh) + sizeof(struct gfs_dinode), 0,
	       BH_SIZE(dibh) - sizeof(struct gfs_dinode));

	memset(&di, 0, sizeof(struct gfs_dinode));

	gfs_meta_header_in(&di.di_header, BH_DATA(dibh));

	di.di_num = *inum;

	if (dip && (dip->i_di.di_mode & 02000))
	{
		di.di_mode = mode | ((type == GFS_FILE_DIR) ? 02000 : 0);
		di.di_gid = dip->i_di.di_gid;
	}
	else
	{
		di.di_mode = mode;
		di.di_gid = osi_cred_to_gid(cred);
	}

	di.di_uid = osi_cred_to_uid(cred);
	di.di_nlink = 1;
	di.di_blocks = 1;
	di.di_atime = di.di_mtime = di.di_ctime = osi_current_time();

	rgd = fs_blk2rgrpd(sdp, inum->no_addr);
	if(!rgd){
		fprintf(stderr, "Unable to map block #%"PRIu64" to rgrp\n", inum->no_addr);
		exit(1);
	}

	di.di_rgrp = rgd->rd_ri.ri_addr;
	di.di_goal_rgrp = di.di_rgrp;
	di.di_goal_dblk = di.di_goal_mblk = inum->no_addr - rgd->rd_ri.ri_data1;

	di.di_type = type;

	gfs_dinode_out(&di, BH_DATA(dibh));
	if(write_buf(sdp, dibh, 0)){
		fprintf(stderr, "make_dinode:  bad write_buf()\n");
		error = -EIO;
	}

	relse_buf(sdp, dibh);


 out:

	return error;
}


int create_inode(struct fsck_sb *sbp, unsigned int type, struct fsck_inode **ip)
{
	uint64_t block;
	struct fsck_rgrp *rgd;
	osi_list_t *tmp;
	struct gfs_inum inum;
	int allocate=0;
	unsigned int mode = 0755;
	osi_cred_t cred;
	cred.cr_uid = getuid();
	cred.cr_gid = getgid();
 retry:
	inum.no_addr = inum.no_formal_ino = 0;
	/* Search for a resource group that has free space */
	osi_list_foreach(tmp, &sbp->rglist) {
		/* Create a new inode in that rgd */
		rgd = osi_list_entry(tmp, struct fsck_rgrp, rd_list);
		if(fs_rgrp_read(rgd)) {
			stack;
			return -1;
		}
		if(rgd->rd_rg.rg_freemeta){
			block = fs_blkalloc_internal(rgd, 0,
						     GFS_BLKST_FREEMETA, GFS_BLKST_USEDMETA, 1);
			block += rgd->rd_ri.ri_data1;

			inum.no_addr = inum.no_formal_ino = block;
			/* FIXME: type isn't right */
			block_set(sbp->bl, block, type);
			/* write out the rgrp */
			gfs_rgrp_out(&rgd->rd_rg, BH_DATA(rgd->rd_bh[0]));
			write_buf(sbp, rgd->rd_bh[0], 0);
			fs_rgrp_relse(rgd);
			break;
		}
		else {
			if(allocate && !clump_alloc(rgd, 0)){
				block = fs_blkalloc_internal(rgd, 0,
							     GFS_BLKST_FREEMETA,
							     GFS_BLKST_USEDMETA, 1);
				block += rgd->rd_ri.ri_data1;

				inum.no_addr = inum.no_formal_ino = block;

				/* FIXME: type isn't right */
				block_set(sbp->bl, block, type);

				/* write out the rgrp */
				gfs_rgrp_out(&rgd->rd_rg, BH_DATA(rgd->rd_bh[0]));
				write_buf(sbp, rgd->rd_bh[0], 0);
				fs_rgrp_relse(rgd);
				break;
			}
			fs_rgrp_relse(rgd);
		}
	}

	if(!inum.no_addr){
		if(allocate){
			fprintf(stderr, "No space available for new file or directory.\n");
			return -1;
		} else {
			allocate = 1;
			goto retry;
		}
	}

        /* We need to setup the inode without attaching it to a directory */
	if(make_dinode(NULL, sbp, &inum, type, mode, &cred)) {
		stack;
		return -1;
	}
	if(fs_get_istruct(sbp, &inum, 1, ip)) {
		stack;
		return -1;
	}

	return 0;


}

