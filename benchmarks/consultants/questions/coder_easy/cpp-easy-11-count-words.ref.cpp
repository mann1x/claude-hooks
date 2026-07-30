#include <iostream>
#include <string>
int main() {
    std::string w;
    int count = 0;
    while (std::cin >> w) count++;
    std::cout << count << std::endl;
    return 0;
}
