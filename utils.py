from argparse import ArgumentParser
import pathlib
from pathlib import Path
import logging
import json
import random
import numpy as np
from tempfile import TemporaryDirectory
from collections import namedtuple
from tqdm import tqdm
import torch
from torch.utils.data import Dataset


def init_logger(args):
    log_format = '%(asctime)-10s: %(message)s'
    if args.log_file is not None and args.log_file != "":
        pathlib.Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, filename=args.log_file, filemode='w', format=log_format)
        logging.warning(f'This will get logged to file: {args.log_file}')
    else:
        logging.basicConfig(level=logging.INFO, format=log_format)


def get_args_parser_with_general_args():
    parser = ArgumentParser()
    parser.add_argument('--pregenerated_data', type=Path, required=True)
    parser.add_argument('--output_dir', type=Path, required=True)

    parser.add_argument("--bert_model", type=str, required=True, help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--do_lower_case", action="store_true")
    parser.add_argument("--reduce_memory", action="store_true",
                        help="Store training data as on-disc memmaps to massively reduce memory usage")

    parser.add_argument("--epochs", type=int, default=3, help="Number of epochs to train for")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--warmup_steps", 
                        default=0, 
                        type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--adam_epsilon", 
                        default=1e-8, 
                        type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--learning_rate",
                        default=3e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--log-file', default=None, type=str)
    return parser


def make_output_dir(args):
    if args.output_dir.is_dir() and list(args.output_dir.iterdir()):
        logging.warning(f"Output directory ({args.output_dir}) already exists and is not empty!")
    args.output_dir.mkdir(parents=True, exist_ok=True)


def get_samples_per_epoch(args):
    samples_per_epoch = []
    for i in range(args.epochs):
        epoch_file = args.pregenerated_data / f"epoch_{i}.json"
        metrics_file = args.pregenerated_data / f"epoch_{i}_metrics.json"
        if epoch_file.is_file() and metrics_file.is_file():
            metrics = json.loads(metrics_file.read_text())
            samples_per_epoch.append(metrics['num_training_examples'])
        else:
            if i == 0:
                exit("No training data was found!")
            print(f"Warning! There are fewer epochs of pregenerated data ({i}) than training epochs ({args.epochs}).")
            print("This script will loop over the available data, but training diversity may be negatively impacted.")
            num_data_epochs = i
            break
    else:
        num_data_epochs = args.epochs

    return samples_per_epoch, num_data_epochs

def set_seeds(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)


InputFeatures = namedtuple("InputFeatures", "input_ids input_mask segment_ids lm_label_ids is_next")


def convert_example_to_features(example, tokenizer, max_seq_length):
    tokens = example["tokens"]
    segment_ids = example["segment_ids"]
    is_random_next = example["is_random_next"]
    masked_lm_positions = example["masked_lm_positions"]
    masked_lm_labels = example["masked_lm_labels"]

    assert len(tokens) == len(segment_ids) <= max_seq_length  # The preprocessed data should be already truncated
    input_ids = tokenizer.convert_tokens_to_ids(tokens)
    masked_label_ids = tokenizer.convert_tokens_to_ids(masked_lm_labels)

    input_array = np.zeros(max_seq_length, dtype=np.int)
    input_array[:len(input_ids)] = input_ids

    mask_array = np.zeros(max_seq_length, dtype=np.bool)
    mask_array[:len(input_ids)] = 1

    segment_array = np.zeros(max_seq_length, dtype=np.bool)
    segment_array[:len(segment_ids)] = segment_ids

    lm_label_array = np.full(max_seq_length, dtype=np.int, fill_value=-1)
    lm_label_array[masked_lm_positions] = masked_label_ids

    features = InputFeatures(input_ids=input_array,
                             input_mask=mask_array,
                             segment_ids=segment_array,
                             lm_label_ids=lm_label_array,
                             is_next=is_random_next)
    return features


class PregeneratedDataset(Dataset):
    def __init__(self, training_path, epoch, tokenizer, num_data_epochs, reduce_memory=False):
        self.vocab = tokenizer.vocab
        self.tokenizer = tokenizer
        self.epoch = epoch
        self.data_epoch = epoch % num_data_epochs
        data_file = training_path / f"epoch_{self.data_epoch}.json"
        metrics_file = training_path / f"epoch_{self.data_epoch}_metrics.json"
        assert data_file.is_file() and metrics_file.is_file()
        metrics = json.loads(metrics_file.read_text())
        num_samples = metrics['num_training_examples']
        seq_len = metrics['max_seq_len']
        self.temp_dir = None
        self.working_dir = None
        if reduce_memory:
            self.temp_dir = TemporaryDirectory()
            self.working_dir = Path(self.temp_dir.name)
            input_ids = np.memmap(filename=self.working_dir/'input_ids.memmap',
                                  mode='w+', dtype=np.int32, shape=(num_samples, seq_len))
            input_masks = np.memmap(filename=self.working_dir/'input_masks.memmap',
                                    shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
            segment_ids = np.memmap(filename=self.working_dir/'segment_ids.memmap',
                                    shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
            lm_label_ids = np.memmap(filename=self.working_dir/'lm_label_ids.memmap',
                                     shape=(num_samples, seq_len), mode='w+', dtype=np.int32)
            lm_label_ids[:] = -1
            is_nexts = np.memmap(filename=self.working_dir/'is_nexts.memmap',
                                 shape=(num_samples,), mode='w+', dtype=np.bool)
        else:
            input_ids = np.zeros(shape=(num_samples, seq_len), dtype=np.int32)
            input_masks = np.zeros(shape=(num_samples, seq_len), dtype=np.bool)
            segment_ids = np.zeros(shape=(num_samples, seq_len), dtype=np.bool)
            lm_label_ids = np.full(shape=(num_samples, seq_len), dtype=np.int32, fill_value=-1)
            is_nexts = np.zeros(shape=(num_samples,), dtype=np.bool)
        logging.info(f"Loading training examples for epoch {epoch} from {data_file}")
        with data_file.open() as f:
            for i, line in enumerate(tqdm(f, total=num_samples, desc="Training examples")):
                line = line.strip()
                example = json.loads(line)
                features = convert_example_to_features(example, tokenizer, seq_len)
                input_ids[i] = features.input_ids
                segment_ids[i] = features.segment_ids
                input_masks[i] = features.input_mask
                lm_label_ids[i] = features.lm_label_ids
                is_nexts[i] = features.is_next
        assert i == num_samples - 1  # Assert that the sample count metric was true
        logging.info("Loading complete!")
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_ids = input_ids
        self.input_masks = input_masks
        self.segment_ids = segment_ids
        self.lm_label_ids = lm_label_ids
        self.is_nexts = is_nexts

    def __len__(self):
        return self.num_samples

    def __getitem__(self, item):
        return (torch.tensor(self.input_ids[item].astype(np.int64)),
                torch.tensor(self.input_masks[item].astype(np.int64)),
                torch.tensor(self.segment_ids[item].astype(np.int64)),
                torch.tensor(self.lm_label_ids[item].astype(np.int64)),
                torch.tensor(self.is_nexts[item].astype(np.int64)))