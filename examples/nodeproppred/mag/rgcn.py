import copy
import argparse

import torch
import torch.nn.functional as F
from torch.nn import Parameter, ModuleDict, ModuleList, Linear, ParameterDict
from torch_sparse import SparseTensor

from ogb.nodeproppred import PygNodePropPredDataset, Evaluator

from logger import Logger


class RGCNConv(torch.nn.Module):
    def __init__(self, in_channels, out_channels, node_types, edge_types):
        super(RGCNConv, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels

        self.node_types = node_types
        self.edge_types = edge_types

        # `ModuleDict` does not allow tuples :(
        self.rel_lins = ModuleDict({
            f'{key[0]}_{key[1]}_{key[2]}': Linear(in_channels, out_channels,
                                                  bias=False)
            for key in edge_types
        })

        self.root_lins = ModuleDict({
            key: Linear(in_channels, out_channels, bias=True)
            for key in node_types
        })

        self.reset_parameters()

    def reset_parameters(self):
        for lin in self.rel_lins.values():
            lin.reset_parameters()
        for lin in self.root_lins.values():
            lin.reset_parameters()

    def forward(self, x_dict, adj_t_dict):
        out_dict = {}
        for key in self.node_types:
            out_dict[key] = self.root_lins[key](x_dict[key])

        for key in self.edge_types:
            key_str = f'{key[0]}_{key[1]}_{key[2]}'
            x = x_dict[key[0]]
            adj_t = adj_t_dict[key]
            out = self.rel_lins[key_str](adj_t.matmul(x, reduce='mean'))
            out_dict[key[2]].add_(out)

        return out_dict


class RGCN(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, num_layers,
                 dropout, num_nodes_dict, x_types, edge_types, out_type):
        super(RGCN, self).__init__()

        self.out_type = out_type

        node_types = list(num_nodes_dict.keys())

        self.embs = ParameterDict({
            key: Parameter(torch.Tensor(num_nodes_dict[key], hidden_channels))
            for key in set(node_types).difference(set(x_types))
        })

        self.lins = ModuleDict(
            {key: Linear(in_channels, hidden_channels)
             for key in x_types})

        self.convs = ModuleList()
        for _ in range(num_layers - 1):
            self.convs.append(
                RGCNConv(hidden_channels, hidden_channels, node_types,
                         edge_types))

        # We only need to consider output node types in the last layer.
        edge_types = [keys for keys in edge_types if keys[2] == out_type]

        self.convs.append(
            RGCNConv(hidden_channels, out_channels, [out_type], edge_types))

        self.dropout = dropout

        self.reset_parameters()

    def reset_parameters(self):
        for emb in self.embs.values():
            torch.nn.init.xavier_uniform_(emb)
        for lin in self.lins.values():
            lin.reset_parameters()
        for conv in self.convs:
            conv.reset_parameters()

    def forward(self, x_dict, adj_t_dict):
        x_dict = copy.copy(x_dict)

        for key, x in x_dict.items():
            x_dict[key] = self.lins[key](x)

        for key, emb in self.embs.items():
            x_dict[key] = emb

        for conv in self.convs[:-1]:
            x_dict = conv(x_dict, adj_t_dict)
            for key, x in x_dict.items():
                x_dict[key] = F.relu_(x)
                x_dict[key] = F.dropout(x, p=self.dropout,
                                        training=self.training)
        out = self.convs[-1](x_dict, adj_t_dict)[self.out_type]
        return out.log_softmax(dim=-1)


def train(model, x_dict, adj_t_dict, y_true, train_idx, optimizer):
    model.train()

    optimizer.zero_grad()
    out = model(x_dict, adj_t_dict)
    loss = F.nll_loss(out[train_idx], y_true[train_idx].squeeze())
    loss.backward()
    optimizer.step()

    return loss.item()


@torch.no_grad()
def test(model, x_dict, adj_t_dict, y_true, split_idx, evaluator):
    model.eval()

    out = model(x_dict, adj_t_dict).cpu()
    y_pred = out.argmax(dim=-1, keepdim=True).cpu()
    y_true = y_true.cpu()

    train_acc = evaluator.eval({
        'y_true': y_true[split_idx['train']['paper']],
        'y_pred': y_pred[split_idx['train']['paper']],
    })['acc']
    valid_acc = evaluator.eval({
        'y_true': y_true[split_idx['valid']['paper']],
        'y_pred': y_pred[split_idx['valid']['paper']],
    })['acc']
    test_acc = evaluator.eval({
        'y_true': y_true[split_idx['test']['paper']],
        'y_pred': y_pred[split_idx['test']['paper']],
    })['acc']

    return train_acc, valid_acc, test_acc


def main():
    parser = argparse.ArgumentParser(description='OGBN-MAG (Full-Batch)')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--log_steps', type=int, default=1)
    parser.add_argument('--use_sage', action='store_true')
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--hidden_channels', type=int, default=256)
    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('--lr', type=float, default=0.01)
    parser.add_argument('--epochs', type=int, default=300)
    parser.add_argument('--runs', type=int, default=10)
    args = parser.parse_args()
    print(args)

    device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'
    device = torch.device(device)

    dataset = PygNodePropPredDataset(name='ogbn-mag')
    split_idx = dataset.get_idx_split()
    data = dataset[0]

    # We do not consider those attributes for now.
    data.node_year_dict = None
    data.edge_reltype_dict = None

    print(data)

    # TEST CODE ####################################################3
    # data.num_nodes_dict = {
    #     'author': 2000,
    #     'field_of_study': 300,
    #     'institution': 100,
    #     'paper': 1000,
    # }
    # data.x_dict['paper'] = data.x_dict['paper'][:1000]
    # data.y_dict['paper'] = data.y_dict['paper'][:1000]
    # split_idx = {
    #     'train': {
    #         'paper': torch.arange(200)
    #     },
    #     'valid': {
    #         'paper': torch.arange(200) + 200
    #     },
    #     'test': {
    #         'paper': torch.arange(600) + 400
    #     },
    # }

    # Convert to new transposed `SparseTensor` format and add reverse edges.
    data.adj_t_dict = {}
    for keys, (row, col) in data.edge_index_dict.items():
        sizes = (data.num_nodes_dict[keys[0]], data.num_nodes_dict[keys[2]])
        adj = SparseTensor(row=row, col=col, sparse_sizes=sizes)
        # adj = SparseTensor(row=row, col=col)[:sizes[0], :sizes[1]] # TEST
        if keys[0] != keys[2]:
            data.adj_t_dict[keys] = adj.t()
            data.adj_t_dict[(keys[2], 'to', keys[0])] = adj
        else:
            data.adj_t_dict[keys] = adj.to_symmetric()
    data.edge_index_dict = None

    x_types = list(data.x_dict.keys())
    edge_types = list(data.adj_t_dict.keys())

    model = RGCN(data.x_dict['paper'].size(-1), args.hidden_channels,
                 dataset.num_classes, args.num_layers, args.dropout,
                 data.num_nodes_dict, x_types, edge_types, out_type='paper')

    data = data.to(device)
    model = model.to(device)
    train_idx = split_idx['train']['paper'].to(device)

    evaluator = Evaluator(name='ogbn-mag')
    logger = Logger(args.runs, args)

    for run in range(args.runs):
        model.reset_parameters()
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
        for epoch in range(1, 1 + args.epochs):
            loss = train(model, data.x_dict, data.adj_t_dict,
                         data.y_dict['paper'], train_idx, optimizer)
            result = test(model, data.x_dict, data.adj_t_dict,
                          data.y_dict['paper'], split_idx, evaluator)
            logger.add_result(run, result)

            if epoch % args.log_steps == 0:
                train_acc, valid_acc, test_acc = result
                print(f'Run: {run + 1:02d}, '
                      f'Epoch: {epoch:02d}, '
                      f'Loss: {loss:.4f}, '
                      f'Train: {100 * train_acc:.2f}%, '
                      f'Valid: {100 * valid_acc:.2f}% '
                      f'Test: {100 * test_acc:.2f}%')

        logger.print_statistics(run)
    logger.print_statistics()


if __name__ == "__main__":
    main()
