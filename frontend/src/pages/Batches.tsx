import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Plus, Edit2, Trash2, X, Fish, Eye, History, AlertTriangle } from 'lucide-react';
import { batchApi, pondApi } from '../services/api';
import type { Batch, Pond, BatchRevision, InconsistencyReport } from '../types';

const STATUS_ORDER: Record<string, number> = { active: 0, harvested: 1, closed: 2 };
const STATUS_LABELS: Record<string, string> = { active: '养殖中', harvested: '已出塘', closed: '已关闭' };
const CHANGE_TYPE_LABELS: Record<string, string> = {
  create: '创建', update: '更新', rollback: '回退', transfer: '跨塘转移', normalize: '系统归一'
};

const emptyForm = {
  batch_number: '',
  pond_id: '',
  species: '',
  stocking_date: '',
  estimated_harvest_date: '',
  actual_harvest_date: '',
  status: 'active',
  version: 1,
  reason: '',
  transfer_date: ''
};

const Batches: React.FC = () => {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [ponds, setPonds] = useState<Pond[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editingBatch, setEditingBatch] = useState<Batch | null>(null);
  const [formData, setFormData] = useState(emptyForm);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [revisions, setRevisions] = useState<BatchRevision[] | null>(null);
  const [revisionsBatch, setRevisionsBatch] = useState<Batch | null>(null);
  const [inconsistencies, setInconsistencies] = useState<InconsistencyReport | null>(null);
  const [normalizeMessage, setNormalizeMessage] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const [batchesRes, pondsRes, inconsRes] = await Promise.all([
        batchApi.getAll(),
        pondApi.getAll(),
        batchApi.getInconsistencies()
      ]);
      setBatches(batchesRes.data);
      setPonds(pondsRes.data);
      setInconsistencies(inconsRes.data);
    } catch (error) {
      console.error('Error fetching data:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const isRollback = editingBatch
    ? (STATUS_ORDER[formData.status] ?? 0) < (STATUS_ORDER[editingBatch.status] ?? 0)
    : false;
  const pondChanged = editingBatch
    ? formData.pond_id !== '' && parseInt(formData.pond_id) !== editingBatch.pond_id
    : false;

  const extractError = (error: unknown): { message: string; isConflict: boolean } => {
    if (axios.isAxiosError(error) && error.response) {
      const detail = error.response.data?.detail;
      const message = typeof detail === 'string' ? detail : '提交失败，请检查输入';
      return { message, isConflict: error.response.status === 409 };
    }
    return { message: '网络错误，请稍后重试', isConflict: false };
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError(null);
    setConflict(false);
    try {
      const data = {
        batch_number: formData.batch_number,
        pond_id: parseInt(formData.pond_id),
        species: formData.species,
        stocking_date: formData.stocking_date,
        estimated_harvest_date: formData.estimated_harvest_date || undefined,
        actual_harvest_date: formData.actual_harvest_date || undefined,
        status: formData.status
      };

      if (editingBatch) {
        await batchApi.update(editingBatch.id, {
          ...data,
          version: formData.version,
          reason: formData.reason || undefined,
          transfer_date: pondChanged && formData.transfer_date ? formData.transfer_date : undefined
        });
      } else {
        await batchApi.create(data);
      }

      setShowModal(false);
      setEditingBatch(null);
      setFormData(emptyForm);
      fetchData();
    } catch (error) {
      const { message, isConflict } = extractError(error);
      setSubmitError(message);
      setConflict(isConflict);
      if (isConflict) {
        // 版本冲突：后台数据已被他人修改，刷新列表让用户看到最新状态
        fetchData();
      }
    }
  };

  const handleEdit = (batch: Batch) => {
    setEditingBatch(batch);
    setSubmitError(null);
    setConflict(false);
    setFormData({
      batch_number: batch.batch_number,
      pond_id: batch.pond_id.toString(),
      species: batch.species,
      stocking_date: batch.stocking_date,
      estimated_harvest_date: batch.estimated_harvest_date || '',
      actual_harvest_date: batch.actual_harvest_date || '',
      status: batch.status,
      version: batch.version,
      reason: '',
      transfer_date: ''
    });
    setShowModal(true);
  };

  const handleStatusChange = (status: string) => {
    // 回退到养殖中时，实际收获日期一并清空（服务端同样会处理，保持表单一致）
    const clearsHarvest = editingBatch && editingBatch.status !== 'active' && status === 'active';
    setFormData({
      ...formData,
      status,
      actual_harvest_date: clearsHarvest ? '' : formData.actual_harvest_date
    });
  };

  const handleDelete = async (id: number) => {
    if (window.confirm('确定要删除这个批次吗？')) {
      try {
        await batchApi.delete(id);
        fetchData();
      } catch (error) {
        console.error('Error deleting batch:', error);
      }
    }
  };

  const handleShowRevisions = async (batch: Batch) => {
    setRevisionsBatch(batch);
    setRevisions(null);
    try {
      const res = await batchApi.getRevisions(batch.id);
      setRevisions(res.data);
    } catch (error) {
      console.error('Error fetching revisions:', error);
    }
  };

  const handleNormalize = async () => {
    setNormalizeMessage(null);
    try {
      const res = await batchApi.normalize();
      const parts = [`已归一 ${res.data.normalized.length} 个批次`];
      if (res.data.skipped.length > 0) {
        parts.push(`${res.data.skipped.length} 个批次需人工处理（塘口归属问题）`);
      }
      setNormalizeMessage(parts.join('，'));
      fetchData();
    } catch (error) {
      console.error('Error normalizing batches:', error);
    }
  };

  const getPondName = (pondId: number) => {
    const pond = ponds.find(p => p.id === pondId);
    return pond ? pond.name : '未知塘口';
  };

  const getStatusBadge = (status: string) => {
    const styles: Record<string, string> = {
      'active': 'badge-success',
      'harvested': 'badge-info',
      'closed': 'badge-warning'
    };
    return (
      <span className={`badge ${styles[status] || 'badge-info'}`}>
        {STATUS_LABELS[status] || status}
      </span>
    );
  };

  const parseChangedFields = (raw?: string): string[] => {
    if (!raw) return [];
    try {
      return JSON.parse(raw);
    } catch {
      return [];
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">批次管理</h1>
          <p className="text-gray-600 mt-1">管理养殖批次信息，支持批次追溯</p>
        </div>
        <button
          onClick={() => {
            setEditingBatch(null);
            setSubmitError(null);
            setConflict(false);
            setFormData(emptyForm);
            setShowModal(true);
          }}
          className="btn-primary flex items-center space-x-2"
        >
          <Plus size={20} />
          <span>新增批次</span>
        </button>
      </div>

      {inconsistencies && inconsistencies.total > 0 && (
        <div className="card bg-amber-50 border border-amber-200">
          <div className="flex items-start justify-between">
            <div className="flex items-start space-x-3">
              <AlertTriangle className="text-amber-600 mt-0.5" size={20} />
              <div>
                <p className="font-medium text-amber-800">
                  发现 {inconsistencies.total} 个批次存在历史矛盾数据
                </p>
                <ul className="text-sm text-amber-700 mt-1 space-y-1">
                  {inconsistencies.items.map((item) => (
                    <li key={item.batch_id}>
                      <span className="font-medium">{item.batch_number}</span>：
                      {item.issues.map(i => i.message).join('；')}
                      {item.issues.some(i => !i.auto_fixable) && '（需人工处理）'}
                    </li>
                  ))}
                </ul>
                {normalizeMessage && (
                  <p className="text-sm text-green-700 mt-2">{normalizeMessage}</p>
                )}
              </div>
            </div>
            <button onClick={handleNormalize} className="btn-secondary text-sm py-2 whitespace-nowrap">
              一键安全归一
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card bg-green-50">
          <div className="flex items-center space-x-3">
            <Fish className="text-green-600" size={24} />
            <div>
              <p className="text-sm text-green-600">养殖中</p>
              <p className="text-2xl font-bold text-green-700">
                {batches.filter(b => b.status === 'active').length}
              </p>
            </div>
          </div>
        </div>
        <div className="card bg-blue-50">
          <div className="flex items-center space-x-3">
            <Eye className="text-blue-600" size={24} />
            <div>
              <p className="text-sm text-blue-600">已出塘</p>
              <p className="text-2xl font-bold text-blue-700">
                {batches.filter(b => b.status === 'harvested').length}
              </p>
            </div>
          </div>
        </div>
        <div className="card bg-gray-50">
          <div className="flex items-center space-x-3">
            <Eye className="text-gray-600" size={24} />
            <div>
              <p className="text-sm text-gray-600">总批次</p>
              <p className="text-2xl font-bold text-gray-700">
                {batches.length}
              </p>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>批次号</th>
                <th>塘口</th>
                <th>养殖品种</th>
                <th>放苗日期</th>
                <th>预计收获</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.id}>
                  <td className="font-medium text-ocean-700">{batch.batch_number}</td>
                  <td>{getPondName(batch.pond_id)}</td>
                  <td>{batch.species}</td>
                  <td>{batch.stocking_date}</td>
                  <td>{batch.estimated_harvest_date || '-'}</td>
                  <td>{getStatusBadge(batch.status)}</td>
                  <td>
                    <div className="flex items-center space-x-2">
                      <button
                        onClick={() => handleEdit(batch)}
                        className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg transition-colors"
                        title="编辑"
                      >
                        <Edit2 size={18} />
                      </button>
                      <button
                        onClick={() => handleShowRevisions(batch)}
                        className="p-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                        title="变更记录"
                      >
                        <History size={18} />
                      </button>
                      <button
                        onClick={() => handleDelete(batch.id)}
                        className="p-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                        title="删除"
                      >
                        <Trash2 size={18} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {batches.length === 0 && (
                <tr>
                  <td colSpan={7} className="text-center py-8 text-gray-500">
                    暂无批次数据
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                {editingBatch ? '编辑批次' : '新增批次'}
              </h2>
              <button
                onClick={() => setShowModal(false)}
                className="p-2 text-gray-400 hover:text-gray-600"
              >
                <X size={20} />
              </button>
            </div>

            {submitError && (
              <div className={`mb-4 p-3 rounded-lg text-sm ${conflict ? 'bg-orange-100 text-orange-800' : 'bg-red-100 text-red-700'}`}>
                {conflict ? '版本冲突：' : ''}{submitError}
                {conflict && (
                  <button
                    type="button"
                    className="block mt-2 underline"
                    onClick={() => {
                      if (editingBatch) {
                        const latest = batches.find(b => b.id === editingBatch.id);
                        if (latest) handleEdit(latest);
                      }
                    }}
                  >
                    载入最新数据重新编辑
                  </button>
                )}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    批次号 <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.batch_number}
                    onChange={(e) => setFormData({ ...formData, batch_number: e.target.value })}
                    className="input-field"
                    placeholder="如: 20240401-001"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    塘口 <span className="text-red-500">*</span>
                  </label>
                  <select
                    required
                    value={formData.pond_id}
                    onChange={(e) => setFormData({ ...formData, pond_id: e.target.value })}
                    className="select-field"
                  >
                    <option value="">请选择塘口</option>
                    {ponds.map((pond) => (
                      <option
                        key={pond.id}
                        value={pond.id}
                        disabled={pond.status !== 'active' && pond.id !== editingBatch?.pond_id}
                      >
                        {pond.name} ({pond.area}亩){pond.status !== 'active' ? ' - 已停用' : ''}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  养殖品种 <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  required
                  value={formData.species}
                  onChange={(e) => setFormData({ ...formData, species: e.target.value })}
                  className="input-field"
                  placeholder="如: 草鱼、鲫鱼、虾等"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    放苗日期 <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="date"
                    required
                    value={formData.stocking_date}
                    onChange={(e) => setFormData({ ...formData, stocking_date: e.target.value })}
                    className="input-field"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    预计收获日期
                  </label>
                  <input
                    type="date"
                    value={formData.estimated_harvest_date}
                    onChange={(e) => setFormData({ ...formData, estimated_harvest_date: e.target.value })}
                    className="input-field"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    实际收获日期{formData.status === 'harvested' && <span className="text-red-500"> *</span>}
                  </label>
                  <input
                    type="date"
                    value={formData.actual_harvest_date}
                    onChange={(e) => setFormData({ ...formData, actual_harvest_date: e.target.value })}
                    className="input-field"
                    disabled={formData.status === 'active'}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    状态
                  </label>
                  <select
                    value={formData.status}
                    onChange={(e) => handleStatusChange(e.target.value)}
                    className="select-field"
                  >
                    <option value="active">养殖中</option>
                    <option value="harvested">已出塘</option>
                    <option value="closed">已关闭</option>
                  </select>
                </div>
              </div>

              {pondChanged && editingBatch?.status === 'active' && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    转移日期（留空默认为今天）
                  </label>
                  <input
                    type="date"
                    value={formData.transfer_date}
                    onChange={(e) => setFormData({ ...formData, transfer_date: e.target.value })}
                    className="input-field"
                  />
                  <p className="text-xs text-gray-500 mt-1">
                    跨塘转移将校验两个塘口的有效期与目标塘同时段容量
                  </p>
                </div>
              )}

              {isRollback && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    回退原因 <span className="text-red-500">*</span>
                  </label>
                  <textarea
                    required
                    value={formData.reason}
                    onChange={(e) => setFormData({ ...formData, reason: e.target.value })}
                    className="input-field"
                    rows={2}
                    placeholder={`从「${STATUS_LABELS[editingBatch!.status]}」回退到「${STATUS_LABELS[formData.status]}」必须说明原因，将写入审计记录`}
                  />
                </div>
              )}

              <div className="flex justify-end space-x-3 pt-4">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="btn-secondary"
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="btn-primary"
                >
                  {editingBatch ? '保存修改' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {revisionsBatch && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                变更记录 - {revisionsBatch.batch_number}
              </h2>
              <button
                onClick={() => setRevisionsBatch(null)}
                className="p-2 text-gray-400 hover:text-gray-600"
              >
                <X size={20} />
              </button>
            </div>

            {revisions === null ? (
              <p className="text-gray-500">加载中...</p>
            ) : revisions.length === 0 ? (
              <p className="text-gray-500">暂无变更记录</p>
            ) : (
              <div className="space-y-3">
                {revisions.map((rev) => (
                  <div key={rev.id} className="p-3 bg-gray-50 rounded-lg text-sm">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-gray-900">
                        版本 {rev.revision} · {CHANGE_TYPE_LABELS[rev.change_type] || rev.change_type}
                      </span>
                      <span className="text-gray-500">
                        {new Date(rev.created_at).toLocaleString('zh-CN')}
                      </span>
                    </div>
                    {rev.reason && (
                      <p className="text-gray-700 mt-1">原因：{rev.reason}</p>
                    )}
                    {parseChangedFields(rev.changed_fields).length > 0 && (
                      <p className="text-gray-500 mt-1">
                        变更字段：{parseChangedFields(rev.changed_fields).join('、')}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default Batches;
