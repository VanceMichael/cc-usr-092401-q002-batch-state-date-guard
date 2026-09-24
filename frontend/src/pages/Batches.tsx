import React, { useEffect, useMemo, useState } from 'react';
import { Plus, Edit2, Trash2, X, Fish, Eye, AlertTriangle, History, ShieldCheck } from 'lucide-react';
import axios from 'axios';
import { batchApi, pondApi } from '../services/api';
import type { Batch, Pond, BatchVersion, BatchAnomaly, NormalizeReport } from '../types';
import {
  BATCH_STATUS, BATCH_STATUS_LABELS, ALLOWED_TARGET_STATUSES,
  ACTION_LABELS, batchStatusLabel, batchStatusBadge, isReversal, isClosed,
} from '../constants/status';

interface FormState {
  batch_number: string;
  pond_id: string;
  species: string;
  stocking_date: string;
  estimated_harvest_date: string;
  actual_harvest_date: string;
  status: string;
  reason: string;
  operator: string;
}

const EMPTY_FORM: FormState = {
  batch_number: '',
  pond_id: '',
  species: '',
  stocking_date: '',
  estimated_harvest_date: '',
  actual_harvest_date: '',
  status: BATCH_STATUS.ACTIVE,
  reason: '',
  operator: '',
};

const Batches: React.FC = () => {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [ponds, setPonds] = useState<Pond[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editingBatch, setEditingBatch] = useState<Batch | null>(null);
  const [formData, setFormData] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [historyTarget, setHistoryTarget] = useState<Batch | null>(null);
  const [historyRows, setHistoryRows] = useState<BatchVersion[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [anomalies, setAnomalies] = useState<BatchAnomaly[]>([]);
  const [normalizeMsg, setNormalizeMsg] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const [batchesRes, pondsRes, anomaliesRes] = await Promise.all([
        batchApi.getAll(),
        pondApi.getAll(),
        batchApi.anomalies(),
      ]);
      setBatches(batchesRes.data);
      setPonds(pondsRes.data);
      setAnomalies(anomaliesRes.data);
    } catch (error) {
      console.error('Error fetching data:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const validateForm = (): string | null => {
    if (!formData.pond_id) return '请选择塘口';
    if (formData.estimated_harvest_date &&
        formData.estimated_harvest_date < formData.stocking_date) {
      return '预计收获日期不得早于投苗日期';
    }
    if (formData.actual_harvest_date &&
        formData.actual_harvest_date < formData.stocking_date) {
      return '实际收获日期不得早于投苗日期';
    }
    if (formData.actual_harvest_date && formData.estimated_harvest_date &&
        formData.actual_harvest_date < formData.estimated_harvest_date) {
      return '实际收获日期不得早于预计收获日期';
    }
    if (formData.status === BATCH_STATUS.HARVESTED && !formData.actual_harvest_date) {
      return '置为已收获时必须填写实际收获日期';
    }
    if (formData.status === BATCH_STATUS.CLOSED && !formData.actual_harvest_date) {
      return '关闭批次前必须先登记实际收获日期';
    }
    if (formData.status === BATCH_STATUS.ACTIVE && formData.actual_harvest_date) {
      return '养殖中的批次不能保留实际收获日期';
    }
    if (editingBatch && isReversal(editingBatch.status, formData.status) && !formData.reason.trim()) {
      return '回退批次状态必须填写原因';
    }
    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const error = validateForm();
    if (error) {
      setFormError(error);
      return;
    }
    setSaving(true);
    try {
      const payload = {
        batch_number: formData.batch_number,
        pond_id: parseInt(formData.pond_id),
        species: formData.species,
        stocking_date: formData.stocking_date,
        estimated_harvest_date: formData.estimated_harvest_date || null,
        actual_harvest_date: formData.actual_harvest_date || null,
        status: formData.status,
        operator: formData.operator.trim() || undefined,
      };

      if (editingBatch) {
        await batchApi.update(editingBatch.id, {
          ...payload,
          expected_version: editingBatch.version,
          reason: formData.reason.trim() || undefined,
        });
      } else {
        await batchApi.create(payload);
      }

      setShowModal(false);
      setEditingBatch(null);
      setFormData(EMPTY_FORM);
      fetchData();
    } catch (err) {
      if (axios.isAxiosError(err) && err.response?.status === 409) {
        const detail = err.response.data?.detail;
        const serverVersion = detail?.current_version;
        setFormError(
          `版本冲突:该批次已被其他操作者修改(服务器版本 ${serverVersion ?? '?'})。` +
          '请取消后刷新列表,基于最新版本重新编辑,以免覆盖他人修改。'
        );
        // 冲突后拉取最新状态,让操作者可以重新打开编辑
        fetchData();
      } else if (axios.isAxiosError(err) && err.response?.status === 422) {
        const detail = err.response.data?.detail;
        setFormError(typeof detail === 'string' ? detail : '提交数据未通过状态机校验');
      } else {
        console.error('Error saving batch:', err);
        setFormError('保存失败,请重试');
      }
    } finally {
      setSaving(false);
    }
  };

  // 编辑前总是重新拉取最新详情,避免用列表里的过期行数据提交
  const handleEdit = async (batch: Batch) => {
    setFormError(null);
    try {
      const res = await batchApi.getById(batch.id);
      const fresh = res.data;
      setEditingBatch(fresh);
      setFormData({
        batch_number: fresh.batch_number,
        pond_id: fresh.pond_id.toString(),
        species: fresh.species,
        stocking_date: fresh.stocking_date,
        estimated_harvest_date: fresh.estimated_harvest_date || '',
        actual_harvest_date: fresh.actual_harvest_date || '',
        status: fresh.status,
        reason: '',
        operator: '',
      });
      setShowModal(true);
    } catch (err) {
      console.error('Error loading batch detail:', err);
    }
  };

  const handleCreate = () => {
    setEditingBatch(null);
    setFormError(null);
    setFormData(EMPTY_FORM);
    setShowModal(true);
  };

  const handleDelete = async (id: number) => {
    if (window.confirm('确定要删除这个批次吗?')) {
      try {
        await batchApi.delete(id);
        fetchData();
      } catch (error) {
        console.error('Error deleting batch:', error);
      }
    }
  };

  const handleViewHistory = async (batch: Batch) => {
    setHistoryTarget(batch);
    setHistoryRows([]);
    setHistoryLoading(true);
    try {
      const res = await batchApi.history(batch.id);
      setHistoryRows(res.data);
    } catch (err) {
      console.error('Error loading history:', err);
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleNormalizeAll = async () => {
    if (!window.confirm(`发现 ${anomalies.length} 个存在矛盾的批次,是否执行安全归一?系统将清空倒挂日期并对齐状态,每次修改都会留下审计版本。`)) {
      return;
    }
    try {
      const res = await batchApi.normalizeAll('生产经理');
      const report: NormalizeReport = res.data;
      setNormalizeMsg(`扫描 ${report.scanned} 个批次,归一 ${report.normalized} 个矛盾批次。`);
      fetchData();
    } catch (err) {
      console.error('Error normalizing:', err);
    }
  };

  const getPondName = (pondId: number) => {
    const pond = ponds.find(p => p.id === pondId);
    return pond ? pond.name : '未知塘口';
  };

  // 编辑转移时只允许选择当前启用的塘口;批次当前所在塘即使停用也要显示
  const selectablePonds = useMemo(() => {
    return ponds.filter(p => p.status === 'active' ||
      (editingBatch && p.id === editingBatch.pond_id));
  }, [ponds, editingBatch]);

  const statusOptions = editingBatch
    ? ALLOWED_TARGET_STATUSES[editingBatch.status] || [editingBatch.status]
    : [BATCH_STATUS.ACTIVE];

  const requiresReason = editingBatch
    ? isReversal(editingBatch.status, formData.status)
    : false;

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
          <p className="text-gray-600 mt-1">管理养殖批次信息,状态变更受状态机与版本控制保护</p>
        </div>
        <button onClick={handleCreate} className="btn-primary flex items-center space-x-2">
          <Plus size={20} />
          <span>新增批次</span>
        </button>
      </div>

      {anomalies.length > 0 && (
        <div className="card border-l-4 border-red-500 bg-red-50">
          <div className="flex items-start justify-between">
            <div className="flex items-start space-x-3">
              <AlertTriangle className="text-red-600 flex-shrink-0 mt-1" size={22} />
              <div>
                <h3 className="font-semibold text-red-800">发现 {anomalies.length} 个矛盾批次记录</h3>
                <ul className="mt-2 text-sm text-red-700 list-disc list-inside space-y-1">
                  {anomalies.slice(0, 5).map(a => (
                    <li key={a.batch_id}>
                      <span className="font-medium">{a.batch_number}</span>:{a.issues.join(';')}
                    </li>
                  ))}
                  {anomalies.length > 5 && <li>…等 {anomalies.length} 条</li>}
                </ul>
              </div>
            </div>
            <button onClick={handleNormalizeAll}
              className="btn-secondary flex items-center space-x-2 text-sm whitespace-nowrap">
              <ShieldCheck size={16} />
              <span>安全归一</span>
            </button>
          </div>
          {normalizeMsg && <p className="mt-3 text-sm text-green-700">{normalizeMsg}</p>}
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
              <p className="text-sm text-blue-600">已收获</p>
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
              <p className="text-2xl font-bold text-gray-700">{batches.length}</p>
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
                <th>实际收获</th>
                <th>状态</th>
                <th>版本</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.id} className={batch.data_quality === 'sanitized' ? 'bg-amber-50' : ''}>
                  <td className="font-medium text-ocean-700">
                    {batch.batch_number}
                    {batch.data_quality === 'sanitized' && (
                      <span className="ml-2 text-xs text-amber-700" title="该批次曾存在矛盾数据,已安全归一">已归一</span>
                    )}
                  </td>
                  <td>{getPondName(batch.pond_id)}</td>
                  <td>{batch.species}</td>
                  <td>{batch.stocking_date}</td>
                  <td>{batch.estimated_harvest_date || '-'}</td>
                  <td>{batch.actual_harvest_date || '-'}</td>
                  <td>
                    <span className={`badge ${batchStatusBadge(batch.status)}`}>
                      {batchStatusLabel(batch.status)}
                    </span>
                  </td>
                  <td className="text-gray-500 text-sm">v{batch.version}</td>
                  <td>
                    <div className="flex items-center space-x-2">
                      <button onClick={() => handleEdit(batch)}
                        title="编辑(基于最新版本)"
                        className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg transition-colors">
                        <Edit2 size={18} />
                      </button>
                      <button onClick={() => handleViewHistory(batch)}
                        title="审计版本历史"
                        className="p-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors">
                        <History size={18} />
                      </button>
                      <button onClick={() => handleDelete(batch.id)}
                        className="p-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors">
                        <Trash2 size={18} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {batches.length === 0 && (
                <tr>
                  <td colSpan={9} className="text-center py-8 text-gray-500">暂无批次数据</td>
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
                {editingBatch ? `编辑批次(当前版本 v${editingBatch.version})` : '新增批次'}
              </h2>
              <button onClick={() => setShowModal(false)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>

            {editingBatch && isClosed(editingBatch.status) && (
              <div className="mb-4 p-3 bg-yellow-50 text-yellow-800 rounded-lg text-sm">
                批次已关闭并出塘,核心信息已锁定。如需调整,请先把状态改回「已收获」重开批次并填写原因。
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
                    placeholder="如: 20260401-001"
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
                    {selectablePonds.map((pond) => (
                      <option key={pond.id} value={pond.id}
                        disabled={pond.status !== 'active' && !(editingBatch && pond.id === editingBatch.pond_id)}>
                        {pond.name} ({pond.area}亩, 容量{pond.capacity ?? 1})
                        {pond.status !== 'active' ? ' - 已停用' : ''}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-gray-400 mt-1">跨塘转移会校验两个塘的有效期与同时段容量</p>
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
                  <label className="block text-sm font-medium text-gray-700 mb-1">预计收获日期</label>
                  <input
                    type="date"
                    value={formData.estimated_harvest_date}
                    min={formData.stocking_date || undefined}
                    onChange={(e) => setFormData({ ...formData, estimated_harvest_date: e.target.value })}
                    className="input-field"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">实际收获日期</label>
                  <input
                    type="date"
                    value={formData.actual_harvest_date}
                    min={formData.stocking_date || undefined}
                    onChange={(e) => {
                      const v = e.target.value;
                      setFormData({
                        ...formData,
                        actual_harvest_date: v,
                        // 选择实际收获日期时自动推进状态机到已收获,避免状态/日期矛盾
                        status: v && formData.status === BATCH_STATUS.ACTIVE
                          ? BATCH_STATUS.HARVESTED : formData.status,
                      });
                    }}
                    className="input-field"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">状态</label>
                  <select
                    value={formData.status}
                    onChange={(e) => setFormData({ ...formData, status: e.target.value })}
                    className="select-field"
                  >
                    {statusOptions.map(s => (
                      <option key={s} value={s}>{BATCH_STATUS_LABELS[s]}</option>
                    ))}
                  </select>
                </div>
              </div>

              {requiresReason && (
                <div>
                  <label className="block text-sm font-medium text-red-700 mb-1">
                    回退/重开原因 <span className="text-red-500">*</span>
                  </label>
                  <textarea
                    required
                    value={formData.reason}
                    onChange={(e) => setFormData({ ...formData, reason: e.target.value })}
                    className="input-field"
                    rows={2}
                    placeholder="必须说明为什么把已收获/已关闭批次改回养殖状态,该原因将记入审计版本"
                  />
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">操作者(可选)</label>
                <input
                  type="text"
                  value={formData.operator}
                  onChange={(e) => setFormData({ ...formData, operator: e.target.value })}
                  className="input-field"
                  placeholder="记录本次变更的操作者"
                />
              </div>

              {formError && (
                <div className="p-3 bg-red-100 text-red-700 rounded-lg text-sm whitespace-pre-line">
                  {formError}
                </div>
              )}

              <div className="flex justify-end space-x-3 pt-4">
                <button type="button" onClick={() => setShowModal(false)} className="btn-secondary">
                  取消
                </button>
                <button type="submit" disabled={saving} className="btn-primary">
                  {saving ? '提交中...' : editingBatch ? `保存修改(v${editingBatch.version})` : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {historyTarget && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-2xl mx-4 max-h-[85vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                审计版本历史 - {historyTarget.batch_number}
              </h2>
              <button onClick={() => setHistoryTarget(null)} className="p-2 text-gray-400 hover:text-gray-600">
                <X size={20} />
              </button>
            </div>
            {historyLoading ? (
              <p className="text-gray-500">加载中...</p>
            ) : (
              <div className="space-y-3">
                {historyRows.map(row => (
                  <div key={row.id} className="border rounded-lg p-4">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-2">
                        <span className="badge badge-info">v{row.version}</span>
                        <span className="font-medium">{ACTION_LABELS[row.action] || row.action}</span>
                        {row.from_status && (
                          <span className="text-sm text-gray-500">
                            {batchStatusLabel(row.from_status)} → {batchStatusLabel(row.to_status || '')}
                          </span>
                        )}
                      </div>
                      <span className="text-xs text-gray-400">
                        {new Date(row.created_at).toLocaleString('zh-CN')}
                      </span>
                    </div>
                    {row.reason && (
                      <p className="mt-2 text-sm text-red-700">原因:{row.reason}</p>
                    )}
                    {row.operator && (
                      <p className="mt-1 text-xs text-gray-500">操作者:{row.operator}</p>
                    )}
                    <p className="mt-2 text-xs text-gray-500">
                      快照:{row.snapshot.batch_number} / 塘口 {row.snapshot.pond_id} /
                      投苗 {row.snapshot.stocking_date} / 实获 {row.snapshot.actual_harvest_date || '-'} /
                      {batchStatusLabel(row.snapshot.status)}
                    </p>
                  </div>
                ))}
                {historyRows.length === 0 && <p className="text-gray-500">暂无历史版本</p>}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default Batches;
