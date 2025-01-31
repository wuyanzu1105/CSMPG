"""This model shows an example of using dgl.metapath_reachable_graph on the original heterogeneous
graph.
Because the original HAN implementation only gives the preprocessed homogeneous graph, this model
could not reproduce the result in HAN as they did not provide the preprocessing code, and we
constructed another dataset from ACM with a different set of papers, connections, features and
labels.
"""
import copy

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import time

import dgl
from dgl.nn.pytorch import GATConv, GraphConv


class SemanticAttention(nn.Module):
    def __init__(self, in_size, hidden_size=64):
        super(SemanticAttention, self).__init__()

        self.project = nn.Sequential(
            nn.Linear(in_size, hidden_size),
            nn.Tanh(),
            nn.Linear(hidden_size, 1, bias=False)
        )

    def forward(self, z):
        w = self.project(z).mean(0)  # (M, 1)
        beta = torch.softmax(w, dim=0)  # (M, 1)
        beta = beta.expand((z.shape[0],) + beta.shape)  # (N, M, 1)

        return (beta * z).sum(1)  # (N, D * K)


class HANLayer(nn.Module):
    def __init__(self, in_size, out_size, layer_num_heads, dropout, hidden_size, threshold):
        super(HANLayer, self).__init__()
        self.gat_layers = nn.ModuleDict()
        self.rest_layers = nn.ModuleDict()
        self.mp_weights = nn.ParameterDict()
        self.in_size, self.out_size, self.layer_num_heads, self.dropout = in_size, out_size, layer_num_heads, dropout
        self.semantic_attention = SemanticAttention(in_size=out_size * layer_num_heads)
        self.sg_dict = dict()
        self.large_graph = dict()
        self.threshold = threshold
        self.useless_flag = False

        self.project = nn.Sequential(
            nn.Linear(out_size * layer_num_heads, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 1, bias=False)
        )

    def forward(self, g, h, meta_pathset, optimizer, b_ids, test=False, features_dict=None):
        self.useless_flag = False
        meta_paths = list(tuple(meta_path) for meta_path in meta_pathset)
        semantic_embeddings = []
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        g = g.to(device)
        h = h.to(device)
        b_ids = b_ids.to(device)

        for meta_path in meta_paths[:]:
            mp = list(map(str, meta_path))
            if ''.join(mp) not in self.sg_dict and ''.join(mp) not in self.large_graph:
                tim1 = time.time()
                print("Meta-path: ", str(mp), end=' ')
                graph = self.metapath_reachable_graph(g, meta_path)
                if isinstance(graph, float):
                    self.large_graph[''.join(mp)] = graph
                    meta_paths.remove(meta_path)
                    meta_pathset.remove(list(meta_path))
                    self.useless_flag = True
                    print("Prepare dense meta-path graph: ", time.time() - tim1)
                    continue
                self.sg_dict[''.join(mp)] = graph
                gatconv = nn.ModuleDict({''.join(mp): GATConv(self.in_size, self.out_size, self.layer_num_heads,
                                                              self.dropout, self.dropout,
                                                              allow_zero_in_degree=True).to(device)})

                if isinstance(gatconv, nn.ModuleDict):
                    self.gat_layers.update(gatconv)
                    self.rest_layers.update(copy.deepcopy(gatconv))
                    optimizer.add_param_group({'params': gatconv.parameters()})

                print("Average degree: ", graph.number_of_edges() / graph.number_of_nodes(), end=' ')
                print("Prepare meta-path graph (s): ", time.time() - tim1)
            elif ''.join(mp) in self.large_graph:
                self.useless_flag = True
                meta_pathset.remove(list(meta_path))
                meta_paths.remove(meta_path)

        for i, meta_path in enumerate(meta_paths):
            mp = list(map(str, meta_path))
            graph = self.sg_dict[''.join(mp)]
            sampler = dgl.dataloading.MultiLayerNeighborSampler([500])
            dataloader = dgl.dataloading.NodeDataLoader(
                graph, torch.LongTensor(list(set(b_ids.tolist()))), sampler, device=device,
                batch_size=len(b_ids),
                drop_last=False)
            for input_nodes, output_nodes, blocks in dataloader:
                if features_dict:
                    h = features_dict.get('business_features', h)  # 根据实际情况选择特征
                emb = self.gat_layers[''.join(mp)](blocks[0], h[input_nodes]).flatten(1)
                if emb.shape[0] != len(b_ids):
                    c = torch.zeros((h.shape[0], self.out_size)).to(device)
                    c[output_nodes] = emb
                    emb = c[b_ids]
                semantic_embeddings.append(emb)
        if len(semantic_embeddings) == 0:
            semantic_embeddings.append(torch.rand((h.shape[0], self.out_size * self.layer_num_heads)).to(device))
        semantic_embeddings = torch.stack(semantic_embeddings, dim=1)  # (N, M, D * K)

        return self.semantic_attention(semantic_embeddings)  # (N, D * K)


    def reset(self):
        self.gat_layers.load_state_dict(self.rest_layers.state_dict())

    def metapath_reachable_graph(self, g, metapath):
        adj = 1
        for etype in metapath:
            adj = adj * g.adj(etype=etype, scipy_fmt='csr', transpose=False)

        srctype = g.to_canonical_etype(metapath[0])[0]
        dsttype = g.to_canonical_etype(metapath[-1])[2]

        node_pairs = adj.nonzero()

        density = adj.nnz / adj.shape[0] / adj.shape[0]
        print('Density: ', density, end=' ')

        if density > self.threshold:
            return density

        modified_node_pairs = (node_pairs[0], node_pairs[1])
        if len(metapath) > 1:
            topk_indices = np.argsort(adj.data)[-sum(adj.shape) * 5000:]
            node_pairs = (node_pairs[0][topk_indices], node_pairs[1][topk_indices])
            modified_node_pairs = (node_pairs[0], node_pairs[1])

        print('#Edges: ', len(modified_node_pairs[0]), end=' ')

        new_g = dgl.graph(modified_node_pairs, num_nodes=sum(adj.shape), device='cpu').to_simple()
        new_g = dgl.to_bidirected(new_g)

        return new_g



class HAN(nn.Module):
    def __init__(self, in_size, hidden_size, out_size, num_heads, dropout, threshold=0.75):
        super(HAN, self).__init__()
        self.layers = nn.ModuleList()
        self.layers.append(HANLayer(in_size, hidden_size, num_heads[0], dropout, hidden_size=32, threshold=threshold))
        for l in range(1, len(num_heads)):
            self.layers.append(HANLayer(hidden_size * num_heads[l - 1],
                                        hidden_size, num_heads[l], dropout, hidden_size=32, threshold=threshold))
        self.predict = nn.Linear(hidden_size * num_heads[-1], out_size)

    def forward(self, g, h, meta_paths, optimizer, b_ids, test=False, features_dict=None):
        for gnn in self.layers:
            h = gnn(g.cpu(), h, meta_paths, optimizer, b_ids, test, features_dict=features_dict)
        return self.predict(h)


    def get_embedding(self, g, h, meta_paths, optimizer, b_ids, test=False):
        for gnn in self.layers:
            h = gnn(g.cpu(), h, meta_paths, optimizer, b_ids, test, features_dict=self.data.features_dict)
        return h

    def reset(self):
        for gnn in self.layers:
            gnn.reset()

