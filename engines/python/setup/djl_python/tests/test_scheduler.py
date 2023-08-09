import unittest
from collections import defaultdict

from djl_python.scheduler.lm_block import FalconBlock, HuggingfaceBlock
from djl_python.scheduler.utils import compute_offsets, compute_position_ids, compute_attention_mask, merge_tensors, \
    trim_tensor, compute_kv_cache
from djl_python.scheduler.seq_batch_scheduler import SeqBatchScheduler
from djl_python.scheduler.seq_batcher_impl import ContrastiveSeqBatcher
from transformers import AutoConfig, AutoTokenizer, AutoModelForCausalLM
from djl_python.scheduler.search_config import SearchConfig
import torch

from transformers import GPT2LMHeadModel, GPT2Tokenizer

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class TestScheduler(unittest.TestCase):

    def test_lm_block(self):
        model_names = [
            "gpt2", "BlackSamorez/falcon-40b-tiny-testing",
            "seanmor5/tiny-llama-test"
        ]
        for model_name in model_names:
            model = AutoModelForCausalLM.from_pretrained(
                model_name,
                trust_remote_code=True,
                device_map="auto" if device.type == "cuda" else "cpu")
            model_device = model.device

            lm_block = HuggingfaceBlock(
                model) if 'falcon' not in model_name else FalconBlock(model)

            input_ids_0 = torch.tensor(
                [[40, 2883, 6155, 351, 616, 13779, 3290]])
            seq_len = input_ids_0.shape[1]
            model_input = [
                torch.repeat_interleave(input_ids_0, dim=0,
                                        repeats=2).to(model_device),
                torch.repeat_interleave(torch.arange(seq_len)[None, :],
                                        dim=0,
                                        repeats=2).to(model_device),
                torch.repeat_interleave(torch.ones(seq_len,
                                                   dtype=torch.int64)[None, :],
                                        dim=0,
                                        repeats=2).to(model_device)
            ]

            lm_output = lm_block.forward(*model_input, None)

            # input with kv_cache
            past_key_values = lm_output.past_key_values
            input_ids_1 = torch.tensor([[404], [405]]).to(model_device)
            past_seq = past_key_values[0][0].shape[-2]
            position_ids = torch.tensor([[past_seq],
                                         [past_seq]]).to(model_device)
            attention_mask = torch.ones(2, past_seq + 1,
                                        dtype=torch.int64).to(model_device)
            output1 = lm_block.forward(input_ids_1, position_ids,
                                       attention_mask, past_key_values)

    def test_greedy_scheduler(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        tokenizer = GPT2Tokenizer.from_pretrained(model_id,
                                                  padding_side='left')
        tokenizer.pad_token = "[PAD]"
        lm_block = HuggingfaceBlock(model)

        search_config = SearchConfig()
        search_config.max_new_seqlen = 30
        scheduler = SeqBatchScheduler(lm_block, "greedy", search_config)

        input_ids_0 = tokenizer.encode(
            'Memories follow me left and right. I can',
            return_tensors='pt').to(device)
        request_ids = torch.tensor([[0]])

        # Save a kv_cache to file for later use
        kv_cache_files = ["./kv_cache.pt", "./kv_cache_placeholder.pt"]
        compute_kv_cache(
            torch.repeat_interleave(input_ids_0, dim=0, repeats=2),
            scheduler.lm_block, kv_cache_files, None)

        # Test add request
        scheduler.add_request(input_ids_0, request_ids)

        input_ids = tokenizer([
            r"When your legs don't work like they used to before And I can't sweep you off",
            r"There's a time that I remember, when I did not know"
        ],
                              return_tensors='pt',
                              padding=True).input_ids.to(device)

        # Test merging longer sequences
        request_ids = torch.tensor([[1], [2]])
        scheduler.add_request(input_ids, request_ids)
        for idx, _ in enumerate(scheduler.increment_forward(20)):
            pass

        results = scheduler.results

        assert tokenizer.decode(results[1][:30]) == "When your legs don't work like they used to before " \
                                                    "And I can't sweep you off my feet, I can't do anything about it.\n"
        assert tokenizer.decode(results[2][:30]) == "There's a time that I remember, when I did not " \
                                                    "know what to do with my life. I was in a very bad mood. I was"
        assert tokenizer.decode(results[0][:30]) == "Memories follow me left and right. I can't " \
                                                    "remember the last time I saw a girl in a dress. I can't remember the last time"

        # Load a kv_cache from file and test merging a shorter sequence
        input_ids = tokenizer(
            [r"When your legs don't work", r"'t remember", r""],
            return_tensors='pt',
            padding=True).input_ids.to(device)
        request_ids = torch.tensor([[3], [4], [5]])

        # Load a kv_cache file to simulate a fixed reusable prefix which is pre-calculated
        kv_cache = torch.load(kv_cache_files[0])
        scheduler.add_request(input_ids, request_ids, kv_cache=kv_cache)

        # Test trim_and_collect
        for idx, _ in enumerate(scheduler.increment_forward(100)):
            pass

        results = scheduler.collect_results()
        assert len(results) == 6
        assert tokenizer.decode(results[3][10:30]) == "When your legs don't work, you're going " \
                                                    "to be a little bit more tired. I'm"
        assert tokenizer.decode(
            results[4][10:30]
        ) == "'t remember the last time I saw a girl in a dress. I can't remember the last time"

    def test_sampling_scheduler(self):
        torch.manual_seed(20220611)

        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map='auto' if device.type == 'cuda' else 'cpu')
        tokenizer = GPT2Tokenizer.from_pretrained(model_id)
        lm_block = HuggingfaceBlock(model)

        scheduler = SeqBatchScheduler(lm_block, "greedy", SearchConfig())

        search_config = SearchConfig(max_new_tokens=30,
                                     do_sample=True,
                                     top_k=4)
        PAD = search_config.pad_token_id
        input_ids_0 = tokenizer.encode(
            'Memories follow me left and right. I can',
            return_tensors='pt').to(device)
        request_ids = torch.tensor([[0]])

        # Test add request
        scheduler.add_request(input_ids_0,
                              request_ids,
                              search_configs=[search_config])

        input_ids_1 = tokenizer.encode(
            "When your legs don't work like they used to before And I can't sweep you off",
            return_tensors='pt')
        input_ids_2 = torch.concat([
            torch.tensor([PAD, PAD, PAD, PAD, PAD]),
            tokenizer.encode(
                "There's a time that I remember, when I did not know",
                return_tensors='pt')[0]
        ]).view(1, -1)
        input_ids = torch.concat([input_ids_1, input_ids_2], dim=0).to(device)
        config1 = SearchConfig(do_sample=True, top_k=0, top_p=0.92)
        config2 = SearchConfig(do_sample=False)

        # Test merging longer sequences
        request_ids = torch.tensor([[1], [2]])
        scheduler.add_request(input_ids,
                              request_ids,
                              search_configs=[config1, config2])

        # Inference
        for idx, _ in enumerate(scheduler.increment_forward(20)):
            pass

        results = scheduler.collect_results()
        # TODO: The random seed is fixed by torch.manual_seed(), and the sampling output is indeed fixed. However,
        #  the results are not the same between local test and github CI test. So here the assertion is
        #  disabled. After the random seed is aligned between local test and CI test, this assertion should be added
        #  back.
        assert tokenizer.decode(
            results[2][:30]
        ) == "There's a time that I remember, when I did not know what to do with my life. I was in a very bad mood. I was"

    def test_contrastive_scheduler(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        tokenizer = GPT2Tokenizer.from_pretrained(model_id,
                                                  padding_side='left')
        tokenizer.pad_token = "[PAD]"
        lm_block = HuggingfaceBlock(model)

        config = SearchConfig()
        PAD = config.pad_token_id
        scheduler = SeqBatchScheduler(lm_block, "contrastive", config)

        input_ids = tokenizer.encode(
            'Memories follow me left and right. I can',
            return_tensors='pt').to(device)
        request_ids = torch.tensor([[0]])

        # Save a kv_cache to file for later use
        kv_cache_files = ["./kv_cache.pt", "./kv_cache_placeholder.pt"]
        compute_kv_cache(torch.repeat_interleave(input_ids, dim=0, repeats=2),
                         scheduler.lm_block, kv_cache_files, None)

        # Test init_forward
        scheduler.add_request(input_ids, request_ids)

        # Test merging longer sequences
        input_strs = [
            r"When your legs don't work like they used to before And I can't sweep you off",
            r"There's a time that I remember, when I did not know"
        ]
        input_ids = tokenizer(input_strs, return_tensors='pt',
                              padding=True).input_ids.to(device)
        request_ids = torch.tensor([[1], [2]])
        scheduler.add_request(input_ids, request_ids)

        # Forward pass
        for _ in scheduler.increment_forward(20):
            pass

        results = scheduler.results

        assert tokenizer.decode(
            results[1][:30]
        ) == "When your legs don't work like they used to before And I can't sweep you off my feet, but I can tell you that if you're"

        assert tokenizer.decode(
            results[2][:30]
        ) == "There's a time that I remember, when I did not know what to do with myself. I felt like I was going to die. I didn"

        assert tokenizer.decode(results[0][:30]) == "Memories follow me left and right. I can't remember the last " \
                                                    "time I saw her.\n\n\"What do you mean?\" asked my mother"

        # Load a kv_cache from file
        input_ids = torch.tensor([[2215, 534, 7405, 836, 470, 670],
                                  [PAD, PAD, 1858, 338, 257, 640]]).to(device)
        request_ids = torch.tensor([[3], [4]])

        # The kv_cache_file simulates a fixed resusable prefix whose kv_cache is pre-calculated
        kv_cache = torch.load(kv_cache_files[0])
        scheduler.add_request(input_ids, request_ids, kv_cache=kv_cache)

        # Forward pass
        for _ in scheduler.increment_forward(100):
            pass

        results = scheduler.collect_results()
        assert tokenizer.decode(results[3][10:30]) == "When your legs don't work, I'll tell you how to fix " \
                                                    "them.\n\nI'm"
        assert tokenizer.decode(
            results[4][10:30]
        ) == "There's a time and place where I feel like I'm going to die. It's not that"

        # print
        model_name = 'gpt2'
        tokenizer = GPT2Tokenizer.from_pretrained(model_name)
        for i, ret in results.items():
            print('\n{}:'.format(i), tokenizer.decode(ret))

    def test_inhomogeneous_search_config(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        tokenizer = GPT2Tokenizer.from_pretrained(model_id,
                                                  padding_side='left')
        tokenizer.pad_token = "[PAD]"

        lm_block = HuggingfaceBlock(model)

        default_config = SearchConfig()
        default_config.pad_token_id = 50256
        scheduler = SeqBatchScheduler(lm_block, "contrastive", default_config)

        input = [
            r"When your legs don't work like they used to before And I can't sweep you off",
            r"There's a time that I remember, when I did not know"
        ]
        input_ids = tokenizer(input, return_tensors='pt',
                              padding=True).input_ids.to(device)

        request_ids = torch.tensor([[1], [2]])

        search_config = SearchConfig()
        search_config.max_new_seqlen = 25

        # init_forward
        scheduler.add_request(input_ids,
                              request_ids,
                              search_configs=[default_config, search_config])

        # Forward pass
        for i, _ in enumerate(scheduler.increment_forward(70)):
            pass

        results = scheduler.results
        assert len(results[1]) - len(tokenizer(
            input[0]).input_ids) == default_config.max_new_seqlen
        assert len(results[2]) - len(tokenizer(
            input[1]).input_ids) == search_config.max_new_seqlen

    def test_seq_batcher(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        lm_block = HuggingfaceBlock(model)

        search_config = SearchConfig()
        search_config_dict = defaultdict(lambda: search_config)

        # Test SeqBatcher initialization
        input_ids = torch.tensor(
            [[13579, 1749, 1061, 502, 1364, 290, 826, 13, 314, 460]],
            dtype=torch.int64).to(device)
        request_ids = torch.tensor([[0]]).to(device)
        seq_batcher = ContrastiveSeqBatcher.init_forward(
            input_ids, request_ids, lm_block, search_config_dict)[0]

        # Test SeqBatcher addition
        input_ids_new = torch.tensor([[
            2215, 534, 7405, 836, 470, 670, 588, 484, 973, 284, 878, 843, 314,
            460, 470, 16085, 345, 572
        ]]).to(device)
        request_ids_new = torch.tensor([[1]]).to(device)
        seq_batcher_new = \
            ContrastiveSeqBatcher.init_forward(input_ids_new, request_ids_new, lm_block, search_config_dict)[0]

        # Test SeqBatcher.add_batch
        seq_batcher.add_batch(seq_batcher_new)

        assert torch.all(
            seq_batcher.offsets.to('cpu') == torch.tensor([[0], [8]]))
        assert torch.all(
            seq_batcher.request_uids.to('cpu') == torch.tensor([[1], [0]]))
        assert seq_batcher.seq_len == 18
        assert len(seq_batcher.exit_index) == 0
        assert seq_batcher.batch_size == 2

        # Test collect_and_trim
        seq_batcher.exit_index.add(0)  # suppose the 0th sequence should exit
        seq_batcher.collect_and_trim()
        assert torch.all(seq_batcher.offsets.to('cpu') == torch.tensor([[0]]))
        assert torch.all(
            seq_batcher.request_uids.to('cpu') == torch.tensor([[0]]))
        assert seq_batcher.seq_len == 10
        assert len(seq_batcher.exit_index) == 0
        assert seq_batcher.batch_size == 1

        # Test split
        input_ids_new = torch.tensor([[2215, 534, 7405, 836, 470, 670],
                                      [588, 484, 973, 284, 878,
                                       843]]).to(device)
        request_ids_new = torch.tensor([[6], [7]]).to(device)
        seq_batcher_new = \
            ContrastiveSeqBatcher.init_forward(input_ids_new, request_ids_new, lm_block, search_config_dict)[0]
        seq_batcher.add_batch(seq_batcher_new)
        seq_batcher_list = seq_batcher.split(
            [[0], [1, 2]])  # 0, 1, 2 are indices instead of request_uids
        assert len(seq_batcher_list) == 2
        assert seq_batcher_list[0].batch_size == 1
        assert seq_batcher_list[1].batch_size == 2
        assert torch.all(seq_batcher_list[1].request_uids == request_ids_new)

    def test_multi_seq_batcher(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        tokenizer = GPT2Tokenizer.from_pretrained(model_id,
                                                  padding_side='left')
        tokenizer.pad_token = "[PAD]"

        lm_block = HuggingfaceBlock(model)

        default_config = SearchConfig()
        default_config.pad_token_id = 50256
        scheduler = SeqBatchScheduler(lm_block, "contrastive", default_config)

        # input1
        input = [
            r"When your legs don't work like they used to before And I can't sweep you off"
        ]
        input_ids = tokenizer(input, return_tensors='pt',
                              padding=True).input_ids.to(device)

        request_ids = torch.tensor([[1]]).to(device)

        scheduler.add_request(input_ids, request_ids)

        # input2
        input2 = [r"There's a time that I remember, when I did not know"]
        input_ids = tokenizer(input2, return_tensors='pt',
                              padding=True).input_ids.to(device)

        request_ids = torch.tensor([[2]]).to(device)
        scheduler.add_request(input_ids, request_ids)

        assert len(scheduler.seq_batchers[ContrastiveSeqBatcher]) == 1

        # split
        scheduler.seq_batcher_split(ContrastiveSeqBatcher,
                                    seq_batcher_idx=0,
                                    partitions=[[0], [1]])
        assert len(scheduler.seq_batchers[ContrastiveSeqBatcher]) == 2

    def test_utils(self):
        model_name = 'gpt2'
        tokenizer = GPT2Tokenizer.from_pretrained(model_name,
                                                  padding_side='left')

        input1 = [
            r'DeepMind Company is', r'Memories follow me left and right. I can'
        ]
        tokenizer.pad_token = '[PAD]'
        input_ids1 = tokenizer(input1, return_tensors='pt',
                               padding=True).input_ids.to(device)

        # Test compute_offsets
        offsets = compute_offsets(input_ids1, pad_token_ids=[50256, 50256])
        assert torch.all(offsets.to('cpu') == torch.tensor([[6], [0]]))

        # Test compute_attention_mask
        attention_mask = compute_attention_mask(offsets,
                                                input_ids1.shape[-1],
                                                repeat_offset=2)
        assert torch.all(
            attention_mask.to('cpu') == torch.tensor([
                [0, 0, 0, 0, 0, 0, 1, 1, 1, 1], [0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
            ]))

        # Test compute_position_ids
        position_ids = compute_position_ids(input_ids1.shape[0],
                                            input_ids1.shape[1],
                                            offsets,
                                            past_seq_len=0,
                                            repeat_offset=1)
        assert torch.all(
            position_ids.to('cpu') == torch.tensor([[
                0, 0, 0, 0, 0, 0, 0, 1, 2, 3
            ], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]]))

        # Test merge_tensors
        input2 = 'Fastertransformer is'
        input_ids2 = tokenizer(input2, return_tensors='pt',
                               padding=True).input_ids.to(device)
        merged_tensor = merge_tensors(input_ids1, input_ids2, seq_delta=5)

        assert torch.all(
            merged_tensor.to('cpu') == torch.tensor([[
                50256, 50256, 50256, 50256, 50256, 50256, 29744, 28478, 5834,
                318
            ], [13579, 1749, 1061, 502, 1364, 290, 826, 13, 314, 460
                ], [0, 0, 0, 0, 0, 37, 1603, 7645, 16354, 318]]))

        # Test trim_tensor: removed the second row which has the largest sequence length
        trimmed_tensor = trim_tensor(merged_tensor,
                                     keep_indices=torch.tensor([0, 2]),
                                     trim_seq_len=5)
        assert torch.all(
            trimmed_tensor.to('cpu') == torch.tensor([[
                50256, 29744, 28478, 5834, 318
            ], [37, 1603, 7645, 16354, 318]]))

    def test_lru_kv_cache(self):
        model_id = "gpt2"
        model = GPT2LMHeadModel.from_pretrained(
            model_id, device_map="auto" if device.type == "cuda" else "cpu")
        tokenizer = GPT2Tokenizer.from_pretrained(model_id,
                                                  padding_side='left')
        tokenizer.pad_token = "[PAD]"
        lm_block = HuggingfaceBlock(model)

        search_config = SearchConfig()
        search_config.max_new_seqlen = 30
        scheduler = SeqBatchScheduler(lm_block, "greedy", search_config)

        prompt_ids = tokenizer('Memories follow me left and right. I can',
                               return_tensors='pt',
                               padding=True).input_ids
        prompt_ids = prompt_ids.view(1, -1)
        prompt_ids_dict = {1: prompt_ids, 2: prompt_ids}

        # Load a kv_cache from file and test merging a shorter sequence
        input_ids = tokenizer([r"When your legs don't work", r"'t remember"],
                              return_tensors='pt',
                              padding=True).input_ids.to(device)
        request_ids = torch.tensor([[0], [1]]).to(device)
        search_configs = [SearchConfig(), SearchConfig(use_lru_kv_cache=True)]

        # Load a kv_cache file to simulate a fixed reusable prefix which is pre-calculated
        scheduler.add_request(input_ids,
                              request_ids,
                              search_configs=search_configs,
                              kv_cache_prompt_ids=prompt_ids_dict)

        # Test empty input_ids
        input_ids = tokenizer([r""], return_tensors='pt',
                              padding=True).input_ids.view(1, -1).to(device)
        request_ids = torch.tensor([[2]])
        search_configs = [SearchConfig(use_lru_kv_cache=True)]

        # Load a kv_cache file to simulate a fixed reusable prefix which is pre-calculated
        scheduler.add_request(input_ids,
                              request_ids,
                              search_configs=search_configs,
                              kv_cache_prompt_ids=prompt_ids_dict)

        # Test trim_and_collect
        for idx, _ in enumerate(scheduler.increment_forward(100)):
            pass

        results = scheduler.collect_results()
        assert tokenizer.decode(results[0][:30]) == "When your legs don't work, you can try to get them to " \
                                                    "work.\n\nIf you're not sure how to do this, try this"
        assert tokenizer.decode(
            results[1][10:30]
        ) == "'t remember the last time I saw a girl in a dress. I can't remember the last time"
        assert tokenizer.decode(
            results[2][10:30]
        ) == 'The story of the first time I saw a girl in a hospital. I was in the hospital with'


if __name__ == '__main__':
    unittest.main()
