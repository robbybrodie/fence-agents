/******************************************************************************
*******************************************************************************
**
**  Copyright (C) Sistina Software, Inc.  1997-2003  All rights reserved.
**  Copyright (C) 2004 Red Hat, Inc.  All rights reserved.
**
**  This copyrighted material is made available to anyone wishing to use,
**  modify, copy, or redistribute it subject to the terms and conditions
**  of the GNU General Public License v.2.
**
*******************************************************************************
******************************************************************************/

#include "fs_dir.h"

#include "lost_n_found.h"
#include "link.h"
#include "fs_inode.h"
#include "bio.h"
#include "inode.h"
/* add_inode_to_lf - Add dir entry to lost+found for the inode
 * @ip: inode to add to lost + found
 *
 * This function adds an entry into the lost and found dir
 * for the given inode.  The name of the entry will be
 * "lost_<ip->i_num.no_addr>".
 *
 * Returns: 0 on success, -1 on failure.
 */
int add_inode_to_lf(struct fsck_inode *ip){
	char tmp_name[256];
	struct fsck_inode *lf_ip, *ri;
	osi_filename_t filename;
	osi_buf_t b, *bh = &b;
	struct fsck_inode *mip;
	log_info("Locating/creating l+f directory...\n");

	load_inode(ip->i_sbd, ip->i_sbd->sb.sb_root_di.no_addr, &ri);

	if(fs_mkdir(ri, "l+f", 00700, &lf_ip)){
		log_err("Unable to create/locate l+f directory.\n");
	}
	free_inode(&ri);
	if(!lf_ip){
		log_warn("No l+f directory, can not add inode.\n");
		return -1;
	}
	log_notice("l+f directory at %"PRIu64"\n", lf_ip->i_num.no_addr);
	if(!ip->i_sbd->lf_dip) {
		ip->i_sbd->lf_dip = lf_ip;
		block_set(ip->i_sbd->bl, lf_ip->i_num.no_addr, inode_dir);
		increment_link(ip->i_sbd, lf_ip->i_num.no_addr);
		increment_link(ip->i_sbd, lf_ip->i_num.no_addr);
	}
	else if(lf_ip->i_num.no_addr != ip->i_sbd->lf_dip->i_num.no_addr) {
		log_debug("l+f changed from %"PRIu64" to %"PRIu64"\n",
			  ip->i_sbd->lf_dip->i_num.no_addr, lf_ip->i_num.no_addr);
		ip->i_sbd->lf_dip = lf_ip;
		block_set(ip->i_sbd->bl, lf_ip->i_num.no_addr, inode_dir);
		increment_link(ip->i_sbd, lf_ip->i_num.no_addr);
		increment_link(ip->i_sbd, lf_ip->i_num.no_addr);

		if(get_and_read_buf(ip->i_sbd, lf_ip->i_num.no_addr, &bh, 0)){
			log_err("Unable to retrieve block #%"PRIu64"\n",
				lf_ip->i_num.no_addr);
			block_set(ip->i_sbd->bl, lf_ip->i_num.no_addr, meta_inval);
			return -1;
		}

		if(copyin_inode(ip->i_sbd, bh, &mip)) {
			stack;
			relse_buf(ip->i_sbd, bh);
			return -1;
		}
		mip->i_di.di_entries++;
		gfs_dinode_out(&mip->i_di, BH_DATA(bh));
		write_buf(ip->i_sbd, bh, 0);

		free_inode(&mip);
		relse_buf(ip->i_sbd, bh);

	}

	if(!ip->i_di.di_size){
		log_warn("File has zero size, skipping l+f addition.\n");
		return -1;
	}

	switch(ip->i_di.di_type){
	case GFS_FILE_DIR:
		log_info("Adding .. entry pointing to l+f for %"PRIu64"\n",
			 ip->i_num.no_addr);
		sprintf(tmp_name, "..");
		filename.len = strlen(tmp_name);  /* no trailing NULL */
		filename.name = malloc(sizeof(char) * filename.len);
		memset(filename.name, 0, sizeof(char) * filename.len);
		memcpy(filename.name, tmp_name, filename.len);

		if(fs_dirent_del(ip, NULL, &filename)){
			log_err("add_inode_to_lf:  "
				"Unable to remove \"..\" directory entry.\n");
			free(filename.name);
			return -1;
		}

		if(fs_dir_add(ip, &filename, &(lf_ip->i_num),
			      lf_ip->i_di.di_type)){
			log_err("Failed to link \"..\" entry to l+f directory.\n");
			return -1;
		}

		free(filename.name);
		sprintf(tmp_name, "lost_dir_%"PRIu64, ip->i_num.no_addr);
		break;
	case GFS_FILE_REG:
		sprintf(tmp_name, "lost_file_%"PRIu64, ip->i_num.no_addr);
		break;
	default:
		sprintf(tmp_name, "lost_%"PRIu64, ip->i_num.no_addr);
		break;
	}
	filename.len = strlen(tmp_name);  /* no trailing NULL */
	filename.name = malloc(sizeof(char) * filename.len);
	memset(filename.name, 0, sizeof(char) * filename.len);
	memcpy(filename.name, tmp_name, filename.len);

	if(fs_dir_add(lf_ip, &filename, &(ip->i_num), ip->i_di.di_type)){
		log_err("Failed to add inode #%"PRIu64" to l+f dir.\n",
			ip->i_num.no_addr);
		free(filename.name);
		return -1;
	}
  	increment_link(ip->i_sbd, ip->i_num.no_addr);
	if(ip->i_di.di_type == GFS_FILE_DIR) {
		increment_link(ip->i_sbd, lf_ip->i_num.no_addr);
	}
	free(filename.name);
	return 0;
}
