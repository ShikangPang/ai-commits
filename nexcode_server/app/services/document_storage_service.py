"""
文档存储服务
提供文档内容的数据库存储、版本管理和操作记录功能
"""

import asyncio
from typing import Optional, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime

from app.core.database import get_db
from app.models.document_models import (
    Document, 
    DocumentVersion, 
    DocumentOperation, 
    OperationType,
    DocumentStatus
)


class DocumentStorageService:
    """文档存储服务"""
    
    def __init__(self):
        self.save_queue = asyncio.Queue()
        self.save_task = None
        self._initialized = False
    
    def _ensure_initialized(self):
        """确保服务已初始化"""
        if not self._initialized:
            try:
                self._start_background_save()
                self._initialized = True
            except RuntimeError:
                # 如果没有运行的事件循环，延迟初始化
                pass
    
    def _start_background_save(self):
        """启动后台保存任务"""
        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self._background_save_worker())
    
    async def _background_save_worker(self):
        """后台保存工作器"""
        while True:
            try:
                # 等待保存请求
                save_request = await self.save_queue.get()
                
                if save_request is None:  # 停止信号
                    break
                
                document_id, user_id, content, operation = save_request
                
                # 执行保存
                await self._save_document_content(document_id, user_id, content)
                
                # 如果有操作记录，也保存
                if operation:
                    await self._save_operation(document_id, user_id, operation)
                
                # 标记任务完成
                self.save_queue.task_done()
                
            except Exception as e:
                print(f"❌ 后台保存失败: {e}")
                import traceback
                traceback.print_exc()
    
    async def save_content(self, document_id: int, user_id: int, content: str, operation: Optional[Dict] = None):
        """保存文档内容（异步）"""
        self._ensure_initialized()
        await self.save_queue.put((document_id, user_id, content, operation))
    
    async def _save_document_content(self, document_id: int, user_id: int, content: str):
        """保存文档内容到数据库"""
        try:
            async for db in get_db():
                # 获取当前文档
                stmt = select(Document).where(Document.id == document_id)
                result = await db.execute(stmt)
                document = result.scalar_one_or_none()
                
                if not document:
                    print(f"❌ 文档 {document_id} 不存在")
                    return
                
                # 检查内容是否有变化
                if document.content == content:
                    print(f"📝 文档 {document_id} 内容无变化，跳过保存")
                    return
                
                # 保存旧版本
                old_version = DocumentVersion(
                    document_id=document_id,
                    version_number=document.version,
                    title=document.title,
                    content=document.content,
                    changed_by=document.last_editor_id or document.owner_id,
                    change_description=f"自动保存 - 版本 {document.version}"
                )
                db.add(old_version)
                
                # 更新文档内容
                document.content = content
                document.version += 1
                document.last_editor_id = user_id
                document.updated_at = datetime.utcnow()
                
                await db.commit()
                await db.refresh(document)  # 刷新对象以获取最新数据
                print(f"✅ 文档内容已保存: document_id={document_id}, version={document.version}, content_length={len(content)}")
                break
                
        except Exception as e:
            print(f"❌ 保存文档内容失败: {e}")
            import traceback
            traceback.print_exc()
    
    async def _save_operation(self, document_id: int, user_id: int, operation: Dict):
        """保存操作记录到数据库"""
        try:
            async for db in get_db():
                # 创建操作记录
                operation_record = DocumentOperation(
                    document_id=document_id,
                    user_id=user_id,
                    operation_id=operation.get("id", f"op_{document_id}_{user_id}_{asyncio.get_event_loop().time()}"),
                    sequence_number=operation.get("sequence", 0),
                    operation_type=OperationType.INSERT if operation.get("type") == "insert" else OperationType.DELETE,
                    start_position=operation.get("position", 0),
                    end_position=operation.get("position", 0) + len(operation.get("content", "")),
                    content=operation.get("content", "")
                )
                
                db.add(operation_record)
                await db.commit()
                print(f"✅ 操作已保存: document_id={document_id}, user_id={user_id}, operation_id={operation_record.operation_id}")
                break
                
        except Exception as e:
            print(f"❌ 保存操作记录失败: {e}")
            import traceback
            traceback.print_exc()
    
    async def get_document_versions(self, document_id: int, limit: int = 10) -> List[DocumentVersion]:
        """获取文档版本历史"""
        try:
            async for db in get_db():
                stmt = (
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document_id)
                    .order_by(DocumentVersion.version_number.desc())
                    .limit(limit)
                )
                result = await db.execute(stmt)
                versions = result.scalars().all()
                return list(versions)
        except Exception as e:
            print(f"❌ 获取文档版本失败: {e}")
            return []
    
    async def get_document_operations(self, document_id: int, limit: int = 50) -> List[DocumentOperation]:
        """获取文档操作历史"""
        try:
            async for db in get_db():
                stmt = (
                    select(DocumentOperation)
                    .where(DocumentOperation.document_id == document_id)
                    .order_by(DocumentOperation.created_at.desc())
                    .limit(limit)
                )
                result = await db.execute(stmt)
                operations = result.scalars().all()
                return list(operations)
        except Exception as e:
            print(f"❌ 获取文档操作失败: {e}")
            return []
    
    async def create_version_snapshot(self, document_id: int, user_id: int, description: str):
        """创建版本快照"""
        try:
            async for db in get_db():
                # 获取当前文档
                stmt = select(Document).where(Document.id == document_id)
                result = await db.execute(stmt)
                document = result.scalar_one_or_none()
                
                if not document:
                    print(f"❌ 文档 {document_id} 不存在")
                    return
                
                # 创建版本快照（使用当前版本号）
                version = DocumentVersion(
                    document_id=document_id,
                    version_number=document.version,
                    title=document.title,
                    content=document.content,
                    changed_by=user_id,
                    change_description=description
                )
                
                db.add(version)
                await db.commit()
                print(f"✅ 版本快照已创建: document_id={document_id}, version={document.version}, description={description}")
                break
                
        except Exception as e:
            print(f"❌ 创建版本快照失败: {e}")
            import traceback
            traceback.print_exc()
    
    async def restore_version(self, document_id: int, version_number: int, user_id: int):
        """恢复到指定版本"""
        try:
            async for db in get_db():
                # 获取指定版本
                stmt = select(DocumentVersion).where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.version_number == version_number
                )
                result = await db.execute(stmt)
                version = result.scalar_one_or_none()
                
                if not version:
                    print(f"❌ 版本 {version_number} 不存在")
                    return False
                
                # 获取当前文档
                stmt = select(Document).where(Document.id == document_id)
                result = await db.execute(stmt)
                document = result.scalar_one_or_none()
                
                if not document:
                    print(f"❌ 文档 {document_id} 不存在")
                    return False
                
                # 保存当前版本
                current_version = DocumentVersion(
                    document_id=document_id,
                    version_number=document.version,
                    title=document.title,
                    content=document.content,
                    changed_by=document.last_editor_id or document.owner_id,
                    change_description=f"恢复前版本 - 版本 {document.version}"
                )
                db.add(current_version)
                
                # 恢复内容
                document.content = version.content
                document.version += 1
                document.last_editor_id = user_id
                document.updated_at = datetime.utcnow()
                
                await db.commit()
                print(f"✅ 版本恢复成功: document_id={document_id}, 恢复到版本 {version_number}")
                return True
                
        except Exception as e:
            print(f"❌ 版本恢复失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def get_document_statistics(self, document_id: int) -> Dict:
        """获取文档统计信息"""
        try:
            async for db in get_db():
                # 获取文档信息
                stmt = select(Document).where(Document.id == document_id)
                result = await db.execute(stmt)
                document = result.scalar_one_or_none()
                
                if not document:
                    return {}
                
                # 获取版本数量
                stmt = select(func.count(DocumentVersion.id)).where(DocumentVersion.document_id == document_id)
                result = await db.execute(stmt)
                version_count = result.scalar() or 0
                
                # 获取操作数量
                stmt = select(func.count(DocumentOperation.id)).where(DocumentOperation.document_id == document_id)
                result = await db.execute(stmt)
                operation_count = result.scalar() or 0
                
                return {
                    "document_id": document_id,
                    "title": document.title,
                    "current_version": document.version,
                    "content_length": len(document.content) if document.content else 0,
                    "version_count": version_count,
                    "operation_count": operation_count,
                    "created_at": document.created_at,
                    "updated_at": document.updated_at,
                    "last_editor_id": document.last_editor_id
                }
                
        except Exception as e:
            print(f"❌ 获取文档统计信息失败: {e}")
            return {}
    
    async def cleanup_old_operations(self, document_id: int, keep_count: int = 100):
        """清理旧的操作记录"""
        try:
            async for db in get_db():
                # 获取需要删除的操作ID
                stmt = (
                    select(DocumentOperation.id)
                    .where(DocumentOperation.document_id == document_id)
                    .order_by(DocumentOperation.created_at.desc())
                    .offset(keep_count)
                )
                result = await db.execute(stmt)
                old_operation_ids = result.scalars().all()
                
                if old_operation_ids:
                    # 删除旧操作
                    stmt = select(DocumentOperation).where(DocumentOperation.id.in_(old_operation_ids))
                    result = await db.execute(stmt)
                    old_operations = result.scalars().all()
                    
                    for operation in old_operations:
                        await db.delete(operation)
                    
                    await db.commit()
                    print(f"✅ 清理了 {len(old_operations)} 条旧操作记录")
                else:
                    print(f"📝 无需清理操作记录")
                    
        except Exception as e:
            print(f"❌ 清理旧操作记录失败: {e}")
    
    async def shutdown(self):
        """关闭服务"""
        if self.save_task and not self.save_task.done():
            await self.save_queue.put(None)  # 发送停止信号
            await self.save_task
            print("✅ 文档存储服务已关闭")


# 全局文档存储服务实例
document_storage_service = DocumentStorageService() 