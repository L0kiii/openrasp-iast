#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

"""
Copyright 2017-2019 Baidu Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import time
import peewee
import peewee_async

from core.model import base_model
from core.components import common
from core.components import exceptions
from core.components import rasp_result
from core.components.logger import Logger
from core.components.config import Config


class NewRequestModel(base_model.BaseModel):

    def __init__(self, *args, **kwargs):
        """
        初始化
        """
        super(NewRequestModel, self).__init__(*args, **kwargs)
        self._init_start_id()

    def _create_model(self, db, table_prefix):
        """
        创建数据model
        """
        meta_dict = {
            "database": db,
            "table_name": table_prefix + "_" + "ResultList"
        }
        
        meta = type("Meta", (object, ), meta_dict)
        model_dict = {
            "id": peewee.AutoField(),
            "data": self.LongTextField(),
            # utf8mb4 编码下 1 char = 4 bytes，会导致peewee创建过长的列导致MariaDB产生 1071, Specified key was too long; 错误, max_length不使用255
            "data_hash": peewee.CharField(unique=True,  max_length=63), 
            # scan_status含义： 未扫描：0, 已扫描：1, 正在扫描：2, 扫描中出现错误: 3
            "scan_status": peewee.IntegerField(default=0),
            "time": peewee.IntegerField(default=common.get_timestamp),
            "Meta": meta
        }
        self.ResultList = type("ResultList", (peewee.Model, ), model_dict)
        return self.ResultList

    def _init_start_id(self):
        """
        初始化start_id为未扫描的最小id，未扫描时，值为0

        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        query = self.ResultList.select(peewee.fn.MIN(self.ResultList.id)).where(
            self.ResultList.scan_status != 1
        )
        try:
            result = query.scalar()
        except Exception as e:
            Logger().critical("Database error in _init_start_id method!", exc_info=e)
            raise exceptions.DatabaseError
        if result is None:
            self.start_id = 0
        else:
            self.start_id = result - 1

    def reset_unscanned_item(self):
        """
        重置未扫描的item的status为初始状态码(0)

        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        try:
            self.ResultList.update(scan_status = 0).where(self.ResultList.scan_status > 1).execute()
        except Exception as e:
            Logger().critical("Database error in reset_unscanned_item method!", exc_info=e)
            raise exceptions.DatabaseError

    def get_start_id(self):
        """
        获取当前start_id

        Returns:
            start_id, int类型
        """
        return self.start_id

    async def put(self, rasp_result_ins):
        """
        将rasp_result_ins序列化并插入数据表

        Returns:
            插入成功返回True, 重复返回False
        
        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        try:
            data = {
                "data": rasp_result_ins.dump(),
                "data_hash": rasp_result_ins.get_hash()
            }
            await peewee_async.create_object(self.ResultList, **data)
        except peewee.IntegrityError as e:
            return False
        except Exception as e:
            Logger().critical("Database error in put method!", exc_info=e)
            raise exceptions.DatabaseError
        else:
            return True

    async def get_new_scan(self, count=1):
        """
        获取多条未扫描的请求数据

        Parameters:
            count - 最大获取条数，默认为1

        Returns:
            获取的数据组成的list,每个item为一个dict, [{id:数据id, data:请求数据的json字符串} ... ]
        
        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        result = []
        try:
            # 获取未扫描的最小id
            query = self.ResultList.select().where((
                self.ResultList.id > self.start_id) & (
                self.ResultList.scan_status == 0)
            ).limit(1)
            data = await peewee_async.execute(query)
            if (len(data) == 0):
                return []
            else:
                fetch_star_id = data[0].id 

            # 将要获取的记录标记为扫描中
            query = self.ResultList.update(
                {self.ResultList.scan_status: 2}
            ).where((
                self.ResultList.scan_status == 0) & (
                self.ResultList.id > self.start_id)
            ).order_by(self.ResultList.id).limit(count)
            row_count = await peewee_async.execute(query)
            if (row_count == 0):
                return result

            # 获取标记的记录
            query = self.ResultList.select().where((
                self.ResultList.id >= fetch_star_id) & (
                self.ResultList.scan_status == 2)
            ).order_by(
                self.ResultList.id
            ).limit(row_count)
            data = await peewee_async.execute(query)

            for line in data:
                result.append({
                    "id": line.id,
                    "data": rasp_result.RaspResult(line.data)
                })
            return result

        except Exception as e:
            Logger().critical("Database error in get_new_scan method!", exc_info=e)
            raise exceptions.DatabaseError

    async def mark_result(self, last_id, failed_list):
        """
        将id 小于等于 last_id的result标记为已扫描，更新star_id, 将failed_list中的id标记为失败

        Parameters:
            last_id - 已扫描的最大id
            failed_list - 扫描中出现连接失败的url
        
        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        if last_id > self.start_id:
            # 标记失败的扫描记录
            query = self.ResultList.update({self.ResultList.scan_status: 3}).where((
                self.ResultList.id <= last_id) & (
                self.ResultList.id > self.start_id) & (
                self.ResultList.id << failed_list)
            )
            try:
                await peewee_async.execute(query)
            except Exception as e:
                Logger().critical("Database error in mark_result method!", exc_info=e)
                raise exceptions.DatabaseError

            # 标记已扫描的记录
            query = self.ResultList.update({self.ResultList.scan_status: 1}).where((
                self.ResultList.id <= last_id) & (
                self.ResultList.id > self.start_id) & (
                self.ResultList.scan_status == 2)
            )
            try:
                await peewee_async.execute(query)
            except Exception as e:
                Logger().critical("Database error in mark_result method!", exc_info=e)
                raise exceptions.DatabaseError

            # 更新start_id
            query = self.ResultList.select(peewee.fn.MAX(self.ResultList.id)).where((
                self.ResultList.id > self.start_id) & (
                self.ResultList.scan_status == 1)
            )

            try:
                result = await peewee_async.scalar(query)
            except Exception as e:
                Logger().critical("Database error in mark_result method!", exc_info=e)
                raise exceptions.DatabaseError

            if result is not None:
                self.start_id = result

    async def get_scan_count(self):
        """
        获取扫描进度

        Returns:
            total, count 均为int类型，total为数据总数，count为已扫描条数
        
        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        query = self.ResultList.select(peewee.fn.COUNT(self.ResultList.id)).where(
                self.ResultList.scan_status == 1)
        try:
            result = await peewee_async.scalar(query)
        except Exception as e:
            Logger().critical("Database error in get_scan_count method!", exc_info=e)
            raise exceptions.DatabaseError
        if result is None:
            scanned = 0
        else:
            scanned = result

        query = self.ResultList.select(peewee.fn.COUNT(self.ResultList.id))
        try:
            result = await peewee_async.scalar(query)
        except Exception as e:
            Logger().critical("Database error in get_scan_count method!", exc_info=e)
            raise exceptions.DatabaseError
        if result is None:
            total = 0
        else:
            total = result
        
        return total, scanned

    async def get_last_time(self):
        """
        获取最近一条记录的时间戳, 无记录时返回0

        Returns:
            int, 时间戳
        
        Raises:
            exceptions.DatabaseError - 数据库错误引发此异常
        """
        query = self.ResultList.select().order_by(self.ResultList.time.desc()).limit(1)

        try:
            result = await peewee_async.execute(query)
            data = await peewee_async.execute(query)
        except Exception as e:
            Logger().critical("Database error in get_last_time method!", exc_info=e)
            raise exceptions.DatabaseError

        if len(data) == 0:
            return 0
        else:
            return data[0].time