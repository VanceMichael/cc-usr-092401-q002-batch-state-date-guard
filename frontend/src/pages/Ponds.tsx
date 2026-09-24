import React, { useEffect, useState } from 'react';
import { Plus, Edit2, Trash2, X } from 'lucide-react';
import axios from 'axios';
import { pondApi } from '../services/api';
import type { Pond } from '../types';

const Ponds: React.FC = () => {
  const [ponds, setPonds] = useState<Pond[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editingPond, setEditingPond] = useState<Pond | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    area: '',
    water_depth: '',
    species: '',
    status: 'active',
    active_from: '',
    active_to: '',
    capacity: '1'
  });

  const fetchPonds = async () => {
    try {
      const res = await pondApi.getAll();
      setPonds(res.data);
    } catch (error) {
      console.error('Error fetching ponds:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPonds();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    if (formData.active_from && formData.active_to && formData.active_to < formData.active_from) {
      setFormError('有效期止不得早于有效期起');
      return;
    }
    try {
      const data = {
        ...formData,
        area: parseFloat(formData.area),
        water_depth: parseFloat(formData.water_depth),
        capacity: parseInt(formData.capacity) || 1,
        active_from: formData.active_from || null,
        active_to: formData.active_to || null
      };

      if (editingPond) {
        await pondApi.update(editingPond.id, data);
      } else {
        await pondApi.create(data);
      }

      setShowModal(false);
      setEditingPond(null);
      setFormData({
        name: '', area: '', water_depth: '', species: '', status: 'active',
        active_from: '', active_to: '', capacity: '1'
      });
      fetchPonds();
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 422) {
        const detail = error.response.data?.detail;
        setFormError(typeof detail === 'string' ? detail : '提交数据未通过校验');
      } else {
        console.error('Error saving pond:', error);
        setFormError('保存失败,请重试');
      }
    }
  };

  const handleEdit = (pond: Pond) => {
    setEditingPond(pond);
    setFormError(null);
    setFormData({
      name: pond.name,
      area: pond.area.toString(),
      water_depth: pond.water_depth.toString(),
      species: pond.species || '',
      status: pond.status,
      active_from: pond.active_from || '',
      active_to: pond.active_to || '',
      capacity: (pond.capacity ?? 1).toString()
    });
    setShowModal(true);
  };

  const handleDelete = async (id: number) => {
    if (window.confirm('确定要删除这个塘口吗?')) {
      try {
        await pondApi.delete(id);
        fetchPonds();
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 422) {
          window.alert(error.response.data?.detail || '该塘口存在批次记录,不能删除');
        } else {
          console.error('Error deleting pond:', error);
        }
      }
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
          <h1 className="text-2xl font-bold text-gray-900">塘口管理</h1>
          <p className="text-gray-600 mt-1">管理养殖塘口信息</p>
        </div>
        <button
          onClick={() => {
            setEditingPond(null);
            setFormData({
              name: '', area: '', water_depth: '', species: '', status: 'active',
              active_from: '', active_to: '', capacity: '1'
            });
            setShowModal(true);
          }}
          className="btn-primary flex items-center space-x-2"
        >
          <Plus size={20} />
          <span>新增塘口</span>
        </button>
      </div>

      <div className="card">
        <div className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>塘口名称</th>
                <th>面积(亩)</th>
                <th>水深(米)</th>
                <th>养殖品种</th>
                <th>同时段容量</th>
                <th>有效期</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {ponds.map((pond) => (
                <tr key={pond.id}>
                  <td className="font-medium text-gray-900">{pond.name}</td>
                  <td>{pond.area}</td>
                  <td>{pond.water_depth}</td>
                  <td>{pond.species || '-'}</td>
                  <td>{pond.capacity ?? 1}</td>
                  <td className="text-sm text-gray-500">
                    {pond.active_from || '不限'} ~ {pond.active_to || '不限'}
                  </td>
                  <td>
                    <span className={`badge ${pond.status === 'active' ? 'badge-success' : 'badge-info'}`}>
                      {pond.status === 'active' ? '使用中' : '已停用'}
                    </span>
                  </td>
                  <td>
                    <div className="flex items-center space-x-2">
                      <button
                        onClick={() => handleEdit(pond)}
                        className="p-2 text-ocean-600 hover:bg-ocean-50 rounded-lg transition-colors"
                      >
                        <Edit2 size={18} />
                      </button>
                      <button
                        onClick={() => handleDelete(pond.id)}
                        className="p-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                      >
                        <Trash2 size={18} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {ponds.length === 0 && (
                <tr>
                  <td colSpan={8} className="text-center py-8 text-gray-500">
                    暂无塘口数据
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-md mx-4">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-gray-900">
                {editingPond ? '编辑塘口' : '新增塘口'}
              </h2>
              <button
                onClick={() => setShowModal(false)}
                className="p-2 text-gray-400 hover:text-gray-600"
              >
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  塘口名称 <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  required
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input-field"
                  placeholder="请输入塘口名称"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    面积(亩) <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    required
                    value={formData.area}
                    onChange={(e) => setFormData({ ...formData, area: e.target.value })}
                    className="input-field"
                    placeholder="请输入面积"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    水深(米) <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    required
                    value={formData.water_depth}
                    onChange={(e) => setFormData({ ...formData, water_depth: e.target.value })}
                    className="input-field"
                    placeholder="请输入水深"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  养殖品种
                </label>
                <input
                  type="text"
                  value={formData.species}
                  onChange={(e) => setFormData({ ...formData, species: e.target.value })}
                  className="input-field"
                  placeholder="请输入养殖品种"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  同时段容量(个在养批次)
                </label>
                <input
                  type="number"
                  min={1}
                  value={formData.capacity}
                  onChange={(e) => setFormData({ ...formData, capacity: e.target.value })}
                  className="input-field"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">有效期起</label>
                  <input
                    type="date"
                    value={formData.active_from}
                    onChange={(e) => setFormData({ ...formData, active_from: e.target.value })}
                    className="input-field"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">有效期止</label>
                  <input
                    type="date"
                    value={formData.active_to}
                    onChange={(e) => setFormData({ ...formData, active_to: e.target.value })}
                    className="input-field"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  状态
                </label>
                <select
                  value={formData.status}
                  onChange={(e) => setFormData({ ...formData, status: e.target.value })}
                  className="select-field"
                >
                  <option value="active">使用中</option>
                  <option value="inactive">已停用</option>
                </select>
                <p className="text-xs text-gray-400 mt-1">仍有在养批次的塘口不能停用,请先完成跨塘转移</p>
              </div>

              {formError && (
                <div className="p-3 bg-red-100 text-red-700 rounded-lg text-sm">{formError}</div>
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
                  {editingPond ? '保存修改' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default Ponds;
