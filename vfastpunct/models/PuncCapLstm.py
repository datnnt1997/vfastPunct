from typing import Optional
from transformers import logging, BertModel, BertConfig
from transformers.models.bert.modeling_bert import BertEmbeddings
from torchcrf import CRF

import torch
import torch.nn as nn

logging.set_verbosity_error()


class PuncCapLstmConfig(BertConfig):
    def __init__(self, num_plabels=9, num_clabels=3, **kwargs):
        super().__init__(**kwargs)
        self.num_plabels = num_plabels
        self.num_clabels = num_clabels


class PuncCapBiLstmCrf(nn.Module):
    def __init__(self, config):
        super(PuncCapBiLstmCrf, self).__init__()
        self.embeddings = BertEmbeddings(config)
        self.bilstm = nn.LSTM(input_size=config.hidden_size,
                            hidden_size=config.hidden_size // 2,
                            num_layers=2,
                            batch_first=True,
                            bidirectional=True)
        classifier_dropout = (
            config.classifier_dropout if config.classifier_dropout is not None else config.hidden_dropout_prob
        )
        self.dropout = nn.Dropout(classifier_dropout)
        self.p_classifier = nn.Linear(config.hidden_size, config.num_plabels)
        self.c_classifier = nn.Linear(config.hidden_size, config.num_clabels)
        self.p_crf = CRF(config.num_plabels, batch_first=True)
        self.c_crf = CRF(config.num_clabels, batch_first=True)

    @classmethod
    def from_pretrained(cls, model_name: str, config: PuncCapLstmConfig, from_tf: bool = False):
        model = cls(config)
        model.embeddings = BertModel.from_pretrained(model_name, config=config).embeddings
        return model

    def resize_token_embeddings(self, new_num_tokens: Optional[int] = None) -> nn.Embedding:
        """
        Resizes input token embeddings matrix of the model if `new_num_tokens != config.vocab_size`.

        Takes care of tying weights embeddings afterwards if the model class has a `tie_weights()` method.

        Arguments:
            new_num_tokens (`int`, *optional*):
                The number of new tokens in the embedding matrix. Increasing the size will add newly initialized
                vectors at the end. Reducing the size will remove vectors from the end. If not provided or `None`, just
                returns a pointer to the input tokens `torch.nn.Embedding` module of the model without doing anything.

        Return:
            `torch.nn.Embedding`: Pointer to the input tokens Embeddings Module of the model.
        """
        model_embeds = self._resize_token_embeddings(new_num_tokens)
        if new_num_tokens is None:
            return model_embeds

        # Update base model and current model config
        self.config.vocab_size = new_num_tokens
        self.vocab_size = new_num_tokens

        # Tie weights again if needed
        self.tie_weights()

        return model_embeds

    def forward(self,
                input_ids,
                token_type_ids=None,
                attention_mask=None,
                plabels=None,
                clabels=None,
                valid_ids=None,
                label_masks=None):
        embedding_output =  self.embeddings(
            input_ids=input_ids,
            position_ids=None,
            token_type_ids=token_type_ids,
            inputs_embeds=None,
            past_key_values_length=0,
        )
        seq_output, _ = self.bilstm(embedding_output)

        batch_size, max_len, feat_dim = seq_output.shape
        valid_output = torch.zeros(batch_size, max_len, feat_dim, dtype=torch.float32, device=seq_output.device)
        for i in range(batch_size):
            jj = -1
            for j in range(max_len):
                if valid_ids[i][j].item() == 1:
                    jj += 1
                    valid_output[i][jj] = seq_output[i][j]

        sequence_output = self.dropout(valid_output)

        p_logits = self.p_classifier(sequence_output)
        c_logits = self.c_classifier(sequence_output)

        seq_ptags = self.p_crf.decode(p_logits, mask=label_masks != 0)
        seq_ctags = self.c_crf.decode(c_logits, mask=label_masks != 0)

        if plabels is not None:
            p_log_likelihood = self.p_crf(p_logits, plabels, mask=label_masks.type(torch.uint8))
            c_log_likelihood = self.c_crf(c_logits, clabels, mask=label_masks.type(torch.uint8))
            loss = -1.0 * (p_log_likelihood + c_log_likelihood)
            return loss, seq_ptags, seq_ctags
        else:
            return seq_ptags, seq_ctags


# DEBUG
if __name__ == "__main__":
    from transformers import BertConfig

    model_name = 'bert-base-multilingual-cased'
    config = PuncCapLstmConfig.from_pretrained(model_name, num_plabels=9, num_clabels=3)
    model = PuncCapBiLstmCrf.from_pretrained(model_name, config=config, from_tf=False)
    # model = PuncCapBiLstmCrf(config=config)

    input_ids = torch.randint(0, 3000, [2, 20], dtype=torch.long)
    mask = torch.ones([2, 20], dtype=torch.long)
    plabels = torch.randint(0, 8, [2, 20], dtype=torch.long)
    clabels = torch.randint(0, 2, [2, 20], dtype=torch.long)
    new_plabels = torch.zeros([2, 20], dtype=torch.long)
    new_clabels = torch.zeros([2, 20], dtype=torch.long)
    valid_ids = torch.ones([2, 20], dtype=torch.long)
    label_mask = torch.ones([2, 20], dtype=torch.long)
    valid_ids[:, 0] = 0
    valid_ids[:, 13] = 0
    plabels[:, 0] = 0
    clabels[:, 0] = 0
    label_mask[:, -2:] = 0
    for i in range(len(plabels)):
        idx = 0
        for j in range(len(plabels[i])):
            if valid_ids[i][j] == 1:
                new_plabels[i][idx] = plabels[i][j]
                idx += 1
    for i in range(len(clabels)):
        idx = 0
        for j in range(len(clabels[i])):
            if valid_ids[i][j] == 1:
                new_clabels[i][idx] = clabels[i][j]
                idx += 1
    output = model.forward(input_ids,
                           plabels=new_plabels,
                           clabels=new_clabels,
                           attention_mask=mask,
                           valid_ids=valid_ids, label_masks=label_mask)
    print(plabels)
    print(clabels)
    print(new_plabels)
    print(new_clabels)
    print(label_mask)
    print(valid_ids)
    print(output)
