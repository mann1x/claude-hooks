#include <iostream>
#include <string>
#include <vector>
#include <cctype>
int main(){
    std::string s;
    std::getline(std::cin, s);
    std::vector<long> nums; std::vector<char> ops;
    long cur=0; bool have=false;
    for(char c : s){
        if(isdigit((unsigned char)c)){ cur=cur*10+(c-'0'); have=true; }
        else if(c=='+'||c=='-'||c=='*'){ nums.push_back(cur); cur=0; have=false; ops.push_back(c); }
    }
    if(have || nums.empty()) nums.push_back(cur);
    std::vector<long> rn; std::vector<char> ro;
    rn.push_back(nums[0]);
    for(size_t k=0;k<ops.size();k++){
        if(ops[k]=='*') rn.back()=rn.back()*nums[k+1];
        else { ro.push_back(ops[k]); rn.push_back(nums[k+1]); }
    }
    long total=rn[0];
    for(size_t k=0;k<ro.size();k++){
        if(ro[k]=='+') total+=rn[k+1]; else total-=rn[k+1];
    }
    std::cout << total << "\n";
    return 0;
}
